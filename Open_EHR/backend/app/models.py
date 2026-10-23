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
    # Lab Depth W4 (LAB-D8/D9): QC is keyed to instrument + reagent
    # lot + shift, not analyte alone, and a run against a QC control
    # material stamps the control-LOT record it belongs to (app.
    # lab_reagents.LabControlLot — `control_material` remains the
    # human-facing lot key). All four columns are nullable: pre-W4
    # rows carry none of them and behave exactly as before. Existing
    # databases gain them through the inspector-guarded
    # app.lab_qc.ensure_lab_qc_columns hook (Run-16 rule).
    instrument_id: Mapped[str | None] = mapped_column(String(80),
                                                      nullable=True)
    reagent_lot_key: Mapped[str | None] = mapped_column(String(240),
                                                        nullable=True)
    control_lot_id: Mapped[int | None] = mapped_column(Integer,
                                                       nullable=True)
    shift: Mapped[str | None] = mapped_column(String(40), nullable=True)


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


class LabMicroWorkup(Base):
    """Microbiology culture workup record — LAB-D12 (app.lab_micro).

    One row per microbiology order holding the depth workup body as
    JSON: plating record, structured Gram stain + critical-call
    state, colony workup, isolates with per-isolate AST (MIC/zone,
    breakpoint standard + version, computed interpretation, expert-
    rule traces), MDRO alert references and the finalisation stamp
    the antibiogram periods on. Operational bench data, not a FHIR
    resource — the LabQcRun / LabCriticalNotification precedent: the
    staged DiagnosticReport on the FHIR spine stays the report of
    record, and notification internals never ride on it. A NEW
    table (created by create_all); no existing table gains columns.
    """
    __tablename__ = "lab_micro_workups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             default="", index=True)
    body: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


# ---------------------------------------------------------------------------
# LAB-D13 — transfusion service completion (app.lab_transfusion).
# NEW tables only; no existing table gains a column, so no
# ensure-columns hook is owed for this block.
# ---------------------------------------------------------------------------

class LabTsSpecimen(Base):
    """Type-and-screen specimen validity record — LAB-D13.

    One row per registered T&S specimen: collection time, the
    validity kind (standard / presurgical-extended), the extended-
    eligibility evidence (transfusion / pregnancy in the governed
    lookback window) and the computed expiry the Wave A issue gate
    consults through lab_benches. `screen_observation_id` links the
    specimen to the released antibody-screen Observation it produced.
    """
    __tablename__ = "lab_ts_specimens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    specimen_id: Mapped[str] = mapped_column(String(80), unique=True,
                                             nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)
    order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    screen_observation_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   nullable=False)
    validity_kind: Mapped[str] = mapped_column(String(40), nullable=False,
                                               default="standard")
    transfusion_in_lookback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    pregnancy_in_lookback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    extended_requested: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                     default=False)
    extended_granted: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                   default=False)
    extended_denial_reason: Mapped[str | None] = mapped_column(Text,
                                                               nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class LabAntibodyWorkup(Base):
    """Antibody-identification panel workup — LAB-D13. Panel
    reactions, identified specificities and ruled-out specificities;
    identified clinically-significant specificities are mirrored to
    LabHistoricalAntibody in the same transaction."""
    __tablename__ = "lab_antibody_workups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workup_id: Mapped[str] = mapped_column(String(80), unique=True,
                                           nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)
    method: Mapped[str] = mapped_column(String(60), nullable=False)
    panel_reactions: Mapped[list] = mapped_column(JSON, nullable=False,
                                                  default=list)
    identified_specificities: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list)
    ruled_out: Mapped[list] = mapped_column(JSON, nullable=False,
                                            default=list)
    conclusion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class LabHistoricalAntibody(Base):
    """PERMANENT historical antibody record — LAB-D13. One active row
    per patient + specificity. The Wave A issue gate consults this
    table (lab_benches._historical_antibodies): a historical
    clinically-significant antibody removes electronic-crossmatch
    eligibility and requires an antigen-negative unit attestation,
    even when today's screen is negative."""
    __tablename__ = "lab_historical_antibodies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)
    specificity: Mapped[str] = mapped_column(String(120), nullable=False)
    clinically_significant: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True)
    first_identified_on: Mapped[str | None] = mapped_column(String(20),
                                                            nullable=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False,
                                        default="external-history")
    source_workup_id: Mapped[str | None] = mapped_column(String(80),
                                                         nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                         default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class LabTransfusionRequirement(Base):
    """Standing special transfusion requirement for a patient —
    LAB-D13 (irradiated / cmv-negative / washed / antigen-negative).
    Satisfied per unit by LabUnitAttribute; the gate names any
    unmet active requirement as a failed check."""
    __tablename__ = "lab_transfusion_requirements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)
    requirement_code: Mapped[str] = mapped_column(String(60), nullable=False)
    specificity: Mapped[str | None] = mapped_column(String(120),
                                                    nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                         default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class LabUnitAttribute(Base):
    """Attested special-requirement attribute ON a blood unit —
    LAB-D13 (the unit was irradiated / is CMV-negative / was washed
    / is antigen-negative for a specificity)."""
    __tablename__ = "lab_unit_attributes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unit_id: Mapped[str] = mapped_column(String(80), nullable=False,
                                         index=True)
    attribute_code: Mapped[str] = mapped_column(String(60), nullable=False)
    specificity: Mapped[str | None] = mapped_column(String(120),
                                                    nullable=True)
    attested_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class LabUnitReservation(Base):
    """Crossmatch reservation of a unit for one patient — LAB-D13.
    Status active -> released (issue or manual release, with reason)
    or expired (governed expiry, computed at read). A unit with an
    active reservation for another patient cannot be crossmatched,
    reserved or issued to anyone else."""
    __tablename__ = "lab_unit_reservations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reservation_id: Mapped[str] = mapped_column(String(80), unique=True,
                                                nullable=False, index=True)
    unit_id: Mapped[str] = mapped_column(String(80), nullable=False,
                                         index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)
    order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="active")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    released_by: Mapped[str | None] = mapped_column(String(120),
                                                   nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class LabUnitPreparation(Base):
    """Thaw / preparation workflow record for a unit — LAB-D13.
    Steps are recorded in the governed sequence for the prep kind;
    on completion `prepared_at` + the governed post-thaw
    `expires_at` are stamped, and the gate enforces that expiry."""
    __tablename__ = "lab_unit_preparations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prep_id: Mapped[str] = mapped_column(String(80), unique=True,
                                         nullable=False, index=True)
    unit_id: Mapped[str] = mapped_column(String(80), nullable=False,
                                         index=True)
    prep_kind: Mapped[str] = mapped_column(String(60), nullable=False)
    product_type: Mapped[str] = mapped_column(String(60), nullable=False)
    steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="in-progress")
    prepared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class LabUnitDisposition(Base):
    """Terminal disposition accounting for a unit — LAB-D13
    (transfused / wasted / discarded / returned / expired, with a
    governed wastage reason code where applicable)."""
    __tablename__ = "lab_unit_dispositions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    disposition_id: Mapped[str] = mapped_column(String(80), unique=True,
                                                nullable=False, index=True)
    unit_id: Mapped[str] = mapped_column(String(80), nullable=False,
                                         index=True)
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80),
                                                    nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class LabReactionWorkup(Base):
    """Transfusion-reaction workup — LAB-D13. Built on Wave A
    containment (the unit is quarantined and the patient held before
    a workup opens). Steps complete in the fixed sequence in
    app.lab_transfusion.REACTION_STEPS, each with result + actor;
    severity + imputability grading follows; the haemovigilance
    report id links the artefact filed into the existing
    biological-product-deviation backend."""
    __tablename__ = "lab_reaction_workups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workup_id: Mapped[str] = mapped_column(String(80), unique=True,
                                           nullable=False, index=True)
    unit_id: Mapped[str] = mapped_column(String(80), nullable=False,
                                         index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="open")
    steps: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    severity: Mapped[str | None] = mapped_column(String(40), nullable=True)
    imputability: Mapped[str | None] = mapped_column(String(40),
                                                     nullable=True)
    haemovigilance_report_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class PoctQcRecord(Base):
    """Point-of-care testing QC record — Depth pass (app.lab_benches).

    One row per QC run on a POCT device from the Phase 14 device
    registry: the device key, the control level (from the published
    `terminology/poct-programme` config), and the pass/fail outcome.
    CLIA-style POCT programmes run controls on a defined cadence
    (seeded: every level, every day the device is used); the programme
    endpoints derive "QC current" from these rows, never from a flag a
    caller can assert."""
    __tablename__ = "lab_poct_qc_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(40), nullable=False)
    result: Mapped[str] = mapped_column(String(10), nullable=False)  # pass | fail
    operator: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PoctCompetencyRecord(Base):
    """Point-of-care testing operator competency record — Depth pass
    (app.lab_benches). One row per competency assessment: the staff
    member (staff-directory id), the catalog test code they were
    assessed on, the assessment method, and the assessment date.
    Currency is COMPUTED at read from the published
    `terminology/poct-programme` validity window (CLIA's 6-element
    competency model, assessed at the seeded cadence) — an expired
    assessment is never edited back to life, a new row is recorded."""
    __tablename__ = "lab_poct_competency_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    test_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    assessed_on: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date
    assessor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


class PharmacyLot(Base):
    """Lot-level pharmacy inventory — Phase 11 (app.pharmacy_ops).

    One row per (medication, stock location, lot). This is the DETAIL
    under the Phase 4A PharmacyStock aggregate counter: for every
    stock-tracked medication the aggregate equals the sum of its lots,
    maintained in the same transactions that move either (receiving,
    dispensing, adjustment, witnessed CS waste). Lots carry the expiry
    the aggregate cannot express, which is what makes FEFO allocation,
    expiry worklists and DSCSA traceability possible.
    """
    __tablename__ = "pharmacy_lots"
    __table_args__ = (UniqueConstraint("medication_code", "location", "lot_number"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    medication_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    location: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    lot_number: Mapped[str] = mapped_column(String(120), nullable=False)
    expiry_date: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class PharmacyPurchaseOrder(Base):
    """Procurement purchase order — Phase 11 (app.pharmacy_ops).

    Lines are JSON ([{medication_code, quantity, received_quantity}]);
    receiving is cumulative and can never exceed the ordered quantity
    (refused 422 before any write). Status is derived from the lines:
    ordered -> partially_received -> received.
    """
    __tablename__ = "pharmacy_purchase_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_id: Mapped[str] = mapped_column(String(120), unique=True,
                                       nullable=False, index=True)
    supplier: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ordered")
    lines: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class DscsaLedgerEntry(Base):
    """DSCSA traceability ledger entry — Phase 11 (app.pharmacy_ops).

    One row per traceability event: a receiving line (lot, serials,
    expiry captured from the supplier shipment — the DSCSA transaction
    data) or a dispense (the lot allocated FEFO and the serial(s)
    consumed). The ledger is append-only; `detail` carries the full
    serial list when a line carries more than one unit.
    """
    __tablename__ = "dscsa_ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    medication_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    lot_number: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    serial: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expiry_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reference: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PharmacyCsCount(Base):
    """Controlled-substance shift count — Phase 11 (app.pharmacy_ops).

    Expected (the lot book at that location) vs counted, recorded by a
    counter with a second-person witness (witness != counter, enforced
    server-side). A variance makes the count a DISCREPANCY: the count
    book for that medication+location does not balance until the
    discrepancy is resolved with a recorded reason.
    """
    __tablename__ = "pharmacy_cs_counts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    count_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    medication_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    location: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    expected: Mapped[int] = mapped_column(Integer, nullable=False)
    counted: Mapped[int] = mapped_column(Integer, nullable=False)
    variance: Mapped[int] = mapped_column(Integer, nullable=False)
    counter: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    witness: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="balanced")
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PharmacyCsWaste(Base):
    """Witnessed controlled-substance waste — Phase 11 (app.pharmacy_ops).

    Wasting (a partial dose, a dropped tablet) is a two-person act:
    the waster and a witness who is not the waster. The waste decrements
    the lot (and the aggregate) in the same transaction and is written
    to the hash-chained audit like every other CS movement.
    """
    __tablename__ = "pharmacy_cs_waste"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    waste_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    medication_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    location: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    lot_number: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    wasted_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    witness: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PharmacyCompoundBatch(Base):
    """Sterile-compounding batch record — Phase 11 (app.pharmacy_ops).

    One compounded preparation: ingredients with the lot of each,
    compounded under a USP <797> category from published config; the
    beyond-use datetime is COMPUTED at creation from the category's
    configured BUD hours for the storage condition — never caller-set
    and never a code constant. Release requires a verifier who is not
    the preparer (two-person check).
    """
    __tablename__ = "pharmacy_compound_batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    preparation_name: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    storage: Mapped[str] = mapped_column(String(40), nullable=False)
    compounded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    beyond_use_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    ingredients: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    preparer: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    verifier: Mapped[str | None] = mapped_column(String(120), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="in_preparation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PharmacyVerification(Base):
    """One pharmacist verification of one medication order — Phase 11
    (app.pharmacy_ops). The verification queue is derived (signed/active
    MedicationRequests with no row here); this row is the record that
    the verification happened, by whom, and — for formulary-restricted
    items — that the restriction was explicitly acknowledged.

    Wave A (PHARM-B1/M1): the row also carries its lifecycle state.
    A verification is CURRENT only while status is "active", its
    `order_version` still equals the order's FhirResource.version (an
    edited order invalidates it — stale), and `verified_at` is inside
    the governed validity window (pharmacy-ops config
    `verificationValidityHours`; null = no time expiry). Revocation
    (status "revoked" + revoked_by/at/reason) returns the order to
    the verification queue; re-verification reuses this unique row.
    The DUR override that let verification proceed past a hard-stop
    signal (allergy / monitoring blocker / high-alert) is recorded in
    override_reason_code / override_note / overridden_signals (JSON
    text). The Wave A columns are additive — pre-existing databases
    gain them via pharmacy_ops.ensure_pharmacy_verification_columns.
    """
    __tablename__ = "pharmacy_verifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    medication_code: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    verified_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    restriction_acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="active")
    revoked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    override_reason_code: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    override_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    overridden_signals: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PharmacyDurDisposition(Base):
    """One pharmacist disposition of one DUR signal on one order —
    Wave A (PHARM-M1, app.pharmacy_ops). The DUR queue is computed;
    this row is the durable record that a named pharmacist disposed
    of a signal (override / order-changed / therapy-changed /
    not-applicable) with a governed reason code and a note. A
    recorded "override" disposition satisfies that signal at
    verification time; verify still records that it proceeded on the
    disposition (the signal rides the verification's
    overridden_signals). New table only (Wave A)."""
    __tablename__ = "pharmacy_dur_dispositions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    signal_key: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    disposition: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    reason_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


class AgentEnablement(Base):
    """Operational enablement state for one Studio agent config —
    Phase 10, Track A (app.agents).

    An agent DEFINITION is Studio config (kind=agent: draft -> publish,
    published immutable). Whether the published agent may RUN is a
    separate operational step — enable — mirroring the module-pack
    lifecycle (install-draft -> approve -> attach -> enable): publish
    approves the definition; enable turns it on. One row per agent key;
    the flip itself is hash-chain audited (agents.enable /
    agents.disable), this row is just the current state."""
    __tablename__ = "agent_enablements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_key: Mapped[str] = mapped_column(String(120), unique=True,
                                           nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class AgentRun(Base):
    """One run of one enabled agent — Phase 10, Track A (app.agents).

    Agents draft, humans act: a run produces a DRAFT output and stops.
    The row records the agent key + published config version, the
    actor, the sha256 of the input, the output text, and the human
    disposition once recorded (pending -> accepted / rejected). Every
    run and every disposition also writes a hash-chained AuditLog row;
    this table is the reviewable worklist, the chain is the proof."""
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(120), unique=True,
                                        nullable=False, index=True)
    agent_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    agent_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    disposition: Mapped[str] = mapped_column(String(20), nullable=False,
                                             default="pending")
    disposition_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    disposition_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ComplianceRequirement(Base):
    """One row of the regulatory requirements register — Phase 10,
    Track J (app.compliance).

    The register is DATA: which body requires what, the operational
    control that satisfies it, where that control is implemented, and
    the evidence source (an audit-action prefix or a config object)
    whose records prove the control fired. Survey evidence is then a
    query over the hash-chained audit, not a scramble."""
    __tablename__ = "compliance_requirements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    body: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    citation: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    control: Mapped[str] = mapped_column(Text, nullable=False, default="")
    implementation_ref: Mapped[str] = mapped_column(String(300), nullable=False,
                                                    default="")
    evidence_source: Mapped[str] = mapped_column(String(300), nullable=False,
                                                 default="")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class BreakGlassAccess(Base):
    """One break-the-glass emergency chart access — Phase 10, Track K
    (app.privacy).

    Opening always works in the moment (availability is a safety
    property) and always demands a reason; access is time-boxed by the
    configured duration; EVERY opening lands in the privacy review
    queue and must be reviewed (the 100%-review rule) with the
    justification due inside the configured window (24–48h pattern).
    The opening, its expiry and its review are all hash-chain audited;
    this row is the queue state."""
    __tablename__ = "break_glass_accesses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    access_id: Mapped[str] = mapped_column(String(120), unique=True,
                                           nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    justification_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    review_outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
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


# ---------------------------------------------------------------------------
# Phase 12 — care-continuum operations (app.continuum). Operational
# records beside the FHIR chart: the Encounter / Appointment / Task
# resources stay the clinical record; these tables are the department
# machinery (timers, queues, assignments, safety reports) that the
# Phase 10 roadmap's Track D names. Every row is synthetic-data-only,
# like the rest of the system.
# ---------------------------------------------------------------------------

class EdActivation(Base):
    """Time-critical pathway activation (STEMI / stroke / trauma).

    Started against an ED encounter; the pathway, its target minutes
    and its milestone list are copied from the published
    `terminology/continuum-ops` config at start time (the record keeps
    the targets it was started under even if the config later moves).
    Milestones are stamped as the team records them; a milestone past
    its target is a breach — flagged on read, never hidden.
    """
    __tablename__ = "ed_activations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activation_id: Mapped[str] = mapped_column(String(120), unique=True,
                                                nullable=False, index=True)
    encounter_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    pathway: Mapped[str] = mapped_column(String(60), nullable=False)
    pathway_display: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    target_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    milestones: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="open")
    outcome: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FacilityOpsState(Base):
    """One-row-per-key live operations state (diversion, surge).

    The CURRENT state is a row; every change is also a hash-chained
    audit entry, so the history a surveyor or an after-action review
    needs lives in the audit chain, not in overwritten fields.
    """
    __tablename__ = "facility_ops_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class ResultCallback(Base):
    """Results-callback record: a discharged patient's result was still
    pending; the callback is the tracked act of reaching the patient
    with it. The queue itself is derived (pending results for departed
    patients); this row records that a callback happened, its outcome,
    and who owns any follow-up still owed."""
    __tablename__ = "result_callbacks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[str] = mapped_column(String(120), unique=True,
                                           nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    outcome: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class NurseAssignment(Base):
    """Acuity-based nurse assignment for one active admission.

    One row per admission (upserted as the assignment changes): the
    nurse of record and the patient's current acuity (levels from the
    continuum-ops config). The unit board sums acuity per nurse and
    flags loads above the configured maximum — a planning signal,
    computed on read, never a stored verdict.
    """
    __tablename__ = "nurse_assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encounter_id: Mapped[str] = mapped_column(String(120), unique=True,
                                              nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    unit: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    nurse: Mapped[str] = mapped_column(String(120), nullable=False)
    acuity: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    assigned_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class EvsTask(Base):
    """Environmental-services bed-turnaround task.

    Created automatically when a bed flips to `cleaning` (discharge /
    transfer, via app.inpatient's single bed-status path) and closed
    when the bed returns to `available` or EVS marks it done. The
    turnaround minutes are therefore measured from the same events
    the bed board already records — no parallel clock.
    """
    __tablename__ = "evs_tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    bed_code: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    encounter_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="open")
    released_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TransportRequest(Base):
    """Intra-facility patient transport request (command centre
    dispatch list). States: requested -> assigned -> in-transit ->
    completed, or cancelled from any open state."""
    __tablename__ = "transport_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    from_location: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    to_location: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    priority: Mapped[str] = mapped_column(String(40), nullable=False, default="routine")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="requested")
    transporter: Mapped[str | None] = mapped_column(String(120), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class TransferRequest(Base):
    """Inbound inter-facility transfer request (transfer centre).

    The patient is EXTERNAL (no Patient row exists yet): the intake
    record carries the referring facility's summary. Accepting creates
    the admission Task the bed team works; declining requires a
    reason. Every decision is hash-chain audited.
    """
    __tablename__ = "transfer_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    patient_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    patient_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    patient_sex: Mapped[str | None] = mapped_column(String(20), nullable=True)
    referring_facility: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    referring_clinician: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    service_line: Mapped[str | None] = mapped_column(String(120), nullable=True)
    urgency: Mapped[str] = mapped_column(String(40), nullable=False, default="routine")
    contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admission_task_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class AftercareCall(Base):
    """Post-discharge follow-up call record. The queue is derived from
    A03 discharges whose call window (continuum-ops config, 48-72h by
    default) is open; this row records the call, whether the patient
    was reached, and the scripted outcome captured."""
    __tablename__ = "aftercare_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encounter_id: Mapped[str] = mapped_column(String(120), unique=True,
                                              nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    discharged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="due")
    reached: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    called_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class SafetyEvent(Base):
    """Patient-safety event report (Armstrong Institute pattern).

    Any staff role may file. `confidential` reports shield the
    reporter's identity in every listing and detail view (the row
    keeps it for the audit trail; the API never renders it). Harm
    levels and categories are continuum-ops config taxonomy, not code.
    Reviewer<->reporter follow-up lives in SafetyEventMessage; the
    reporter side of that thread is addressed by role, never by name,
    when the report is confidential.
    """
    __tablename__ = "safety_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    reporter: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    unit: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="other")
    harm_level: Mapped[str] = mapped_column(String(40), nullable=False, default="none")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="reported")
    rca_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rca_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class SafetyEventMessage(Base):
    """One follow-up message on a safety event. `author_role` is
    'reporter' or 'reviewer'; `author` is stored for the audit trail
    and shielded on read under the same rule as the event itself."""
    __tablename__ = "safety_event_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    author: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    author_role: Mapped[str] = mapped_column(String(40), nullable=False, default="reviewer")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Phase 13 — People & data platform (app.people)
# ---------------------------------------------------------------------------

class StaffMember(Base):
    """One person in the workforce directory — Phase 13, Track E
    (app.people). The directory is operational data: who works here,
    in which job role, assigned to which unit and service line, and
    with which dashboard audience. `username` optionally links the
    directory entry to a sign-in identity (dev token or OIDC user) —
    that link is what lets the role-aware landing resolve a person's
    audience dashboard. Synthetic seed data only; credential numbers
    on StaffCredential are synthetic identifiers, never real licence
    numbers."""
    __tablename__ = "staff_members"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                 index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    job_role: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    unit: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # Organisation & Registration Placement pass: the governed
    # placement chain above the unit — region -> location (facility)
    # -> department -> unit, as codes from the published
    # terminology/locations hierarchy (app.org_hierarchy). Nullable:
    # rows predate the pass and are backfilled by
    # org_hierarchy.ensure_org_placement where their unit resolves.
    region: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                               index=True)
    location: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                 index=True)
    department: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                   index=True)
    service_line: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                     index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    audience: Mapped[str | None] = mapped_column(String(60), nullable=True)
    email: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class StaffCredential(Base):
    """One licence/certification held by a staff member — Phase 13,
    Track E. Expiry state is COMPUTED at read time against the
    published workforce config windows (never stored), so a config
    republication re-windows every credential at once.
    `alerted_for_expiry` records the expires_on value the rules-engine
    scan last alerted for: a scan alerts once per credential per
    expiry date (idempotent), and a renewal (a new expires_on)
    re-arms the alert."""
    __tablename__ = "staff_credentials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    credential_id: Mapped[str] = mapped_column(String(120), unique=True,
                                               nullable=False, index=True)
    staff_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    number: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    issued_on: Mapped[str | None] = mapped_column(String(10), nullable=True)
    expires_on: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date
    alerted_for_expiry: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class CredentialVerification(Base):
    """One primary-source verification (PSV) event for a staff
    credential — Wave A, WF-B1 (app.people). A StaffCredential row is
    SELF-ASSERTED (the holder's claim); this row is the medical staff
    office's act of checking that claim against the primary source:
    which source (a code from the published workforce config `psv`
    section), by what method, who verified, when, with what outcome,
    and the evidence reference (document id / confirmation number)
    the verification rests on. Rows are append-only history — a
    credential's verification STATE is computed at read time from the
    latest event (see app.people._verification_block), never stored
    on the credential. `app.privileging.check_privileges` requires
    the computed state to be "verified" (current within the governed
    re-verification window) before a credential satisfies a gate."""
    __tablename__ = "credential_verifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    verification_id: Mapped[str] = mapped_column(String(120), unique=True,
                                                 nullable=False, index=True)
    credential_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                               index=True)
    source_code: Mapped[str] = mapped_column(String(80), nullable=False,
                                             default="")
    method: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    verified_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    verified_on: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date
    outcome: Mapped[str] = mapped_column(String(40), nullable=False,
                                         default="")
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class CredentialExclusionCheck(Base):
    """One exclusion check for a staff member — Wave A, WF-B1
    (app.people). `kind` is a code from the published workforce
    config `psv.exclusionKinds` list (licence-board / npdb / oig /
    sam class): the external registries a credentialing office must
    clear a clinician against. The check itself is a recorded manual
    act (the external query happens outside this system); what is
    modelled here is that it was run, by whom, when, its outcome
    (clear / hit / not-run) and its evidence reference.
    `credential_id` is nullable: a check may be recorded against one
    credential or against the staff member as a whole. A "hit" is
    data, recorded — it is never silently acted on here; its
    consequence surfaces in the computed credential verification
    state ("exclusion-hit") and in `check_privileges`, which names
    the kind. Exclusion state is computed at read time from the
    latest check per kind, never stored."""
    __tablename__ = "credential_exclusion_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    check_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    staff_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    credential_id: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True,
                                                      index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(40), nullable=False,
                                         default="")
    checked_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    checked_on: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class RosterShift(Base):
    """One rostered shift instance — Phase 13, Track E. Shift
    TEMPLATES are published workforce config (code, display, start/end
    times); an instance pins one staff member to one unit on one date
    under one template. Start/end are stored as instants computed from
    the template at creation, so a later template republication never
    rewrites history."""
    __tablename__ = "roster_shifts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shift_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    staff_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    template_code: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                              nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="scheduled")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class OnCallAssignment(Base):
    """One on-call assignment — Phase 13, Track E. A service line has
    a primary (and optionally a backup) on call over a window; the
    "who is on call now" lookup other modules consume resolves the
    assignment whose window covers the instant. Overlapping primaries
    for the same service are refused at creation (409) — an ambiguous
    on-call answer is worse than none."""
    __tablename__ = "on_call_assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[str] = mapped_column(String(120), unique=True,
                                               nullable=False, index=True)
    service_line: Mapped[str] = mapped_column(String(120), nullable=False,
                                              index=True)
    staff_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    role_on_call: Mapped[str] = mapped_column(String(40), nullable=False,
                                              default="primary")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                              nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Phase 14 — devices, IoT, data lake & pipelines (Tracks H + I)
# ---------------------------------------------------------------------------

class Device(Base):
    """One registered device — Phase 14, Track H (app.devices).

    The general device registry: analysers, vitals monitors, storage
    sensors (fridge/freezer), infusion pumps. A device is operational
    data, not a FHIR resource. Calibration / maintenance posture is
    COMPUTED at read from `calibration_due` / `maintenance_due` and the
    published device-type config (calibration interval + worklist
    window) — never stored as a flag, so a republish or a recorded
    calibration changes the answer with no state repair. `hl7_sender`
    is the MSH-3 sending application this device's ORU messages carry
    (the identity the MLLP gate resolves); `mqtt_topic_key` is the
    topic segment its MQTT readings publish under."""
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_key: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    device_type: Mapped[str] = mapped_column(String(60), nullable=False,
                                             index=True)
    location: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    serial: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(30), nullable=False,
                                        default="active", index=True)
    hl7_sender: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                   index=True)
    mqtt_topic_key: Mapped[str | None] = mapped_column(String(120),
                                                       nullable=True)
    calibration_due: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    maintenance_due: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    last_calibrated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class DeviceReading(Base):
    """One sensor reading from a device — Phase 14, Track H.

    Fridge/freezer temperatures and other sensor metrics. Every
    reading points at its raw-zone entry (`raw_entry_id`); `excursion`
    is computed at ingest against the device type's published range
    and stored so the excursion history is itself auditable data.
    Readings are device data, not patient data."""
    __tablename__ = "device_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(60), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=utcnow)
    source: Mapped[str] = mapped_column(String(60), nullable=False,
                                        default="simulated")
    excursion: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                            default=False)
    raw_entry_id: Mapped[int | None] = mapped_column(Integer, nullable=True,
                                                     index=True)


class LakeRawEntry(Base):
    """One immutable raw-zone payload — Phase 14, Track H/I (app.lake).

    The data lake's raw zone: every inbound payload (HL7 message, FHIR
    Observation, device reading, offline batch item) lands here
    AS-RECEIVED with its content hash, source and receive timestamp.
    Rows are append-only: there is no update or delete path anywhere
    in the codebase — a correction is a NEW entry whose
    `supersedes_id` points at the original. The raw row never changes
    after insert, so the pointer to whatever curated record it became
    lives in its own append-only table (LakeLineageLink)."""
    __tablename__ = "lake_raw_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload_type: Mapped[str] = mapped_column(String(40), nullable=False,
                                              index=True)  # hl7 | fhir-observation | device-reading | offline-batch
    payload_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False,
                                              index=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False,
                                        default="")  # mllp | http | mqtt | simulated | offline-queue
    source_label: Mapped[str] = mapped_column(String(40), nullable=False,
                                              default="simulated")
    device_key: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                   index=True)
    interface: Mapped[str | None] = mapped_column(String(120), nullable=True)
    patient_ref: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                    index=True)
    disposition: Mapped[str] = mapped_column(String(40), nullable=False,
                                             default="received", index=True)
    supersedes_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  default=utcnow, index=True)


class LakeLineageLink(Base):
    """Append-only raw -> curated pointer — Phase 14 (app.lake).

    One row per (raw entry, curated record) relationship, written in
    the same transaction that creates the curated record. Because the
    link is its own row, the raw entry stays byte-immutable while the
    lineage graph can still grow (a replay may link one raw entry to
    a second curated record; both links are kept)."""
    __tablename__ = "lake_lineage_links"
    __table_args__ = (UniqueConstraint("raw_entry_id", "curated_ref"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_entry_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    curated_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)  # e.g. DiagnosticReport/<id>, Observation/<id>, DeviceReading/<id>
    link_kind: Mapped[str] = mapped_column(String(40), nullable=False,
                                           default="ingested-as")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class DataQuarantine(Base):
    """One held batch — Phase 14, Tracks H + I.

    Two producers, one workqueue: (a) device ingestion held by the
    calibration gate (source='device-ingest', the raw payload is in
    the lake and referenced by raw_entry_id); (b) a pipeline step
    whose data-quality checks failed (source='pipeline', the batch
    snapshot rides in `detail`). Held data reaches NO curated store
    until a human releases it (release re-runs the held work) or
    rejects it (the hold and its reason stay on record). Every
    transition is audited."""
    __tablename__ = "data_quarantine"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    raw_entry_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pipeline_run_id: Mapped[str | None] = mapped_column(String(60),
                                                        nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="held", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class PipelineRun(Base):
    """One execution of a published pipeline definition — Phase 14,
    Track I (app.pipelines). Runs are created by an operator call (the
    schedule in the definition is configuration data; there is no
    in-process scheduler daemon — see the module docstring)."""
    __tablename__ = "pipeline_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(60), unique=True, nullable=False,
                                        index=True)
    pipeline_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                              index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer, nullable=False,
                                                  default=1)
    trigger: Mapped[str] = mapped_column(String(30), nullable=False,
                                         default="manual")
    window_start: Mapped[str | None] = mapped_column(String(40), nullable=True)
    window_end: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="running", index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         nullable=True)


class PipelineStepRun(Base):
    """One step execution inside a pipeline run — Phase 14, Track I.

    `idempotency_key` is unique across ALL runs: re-running a pipeline
    over the same scope finds the succeeded step run by key and reuses
    its result instead of re-executing (the runner records the new
    step run as status='reused')."""
    __tablename__ = "pipeline_step_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(60), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False,
                                                unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                        nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         nullable=True)


class PipelineDeadLetter(Base):
    """A pipeline step that exhausted its retries — Phase 14, Track I.
    The Phase 8 dead-letter pattern generalised: the failure, its step
    context and the error are kept; replay starts a fresh run of the
    same pipeline (a new run — history is never rewritten)."""
    __tablename__ = "pipeline_dead_letters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    pipeline_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                              index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(60), nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class DedupCandidate(Base):
    """One probable-duplicate patient pair — Phase 14, Track I (MPI).

    Produced by the pipeline dedup-scan step from the deterministic
    matching rules in the published `terminology/mpi-matching` config.
    A candidate is a REVIEW item for a human: the system never merges
    records on its own (merge is a separate governed human action and
    is out of scope for this phase — the candidate records the
    reviewer's disposition only)."""
    __tablename__ = "dedup_candidates"
    __table_args__ = (UniqueConstraint("patient_a", "patient_b"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_a: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    patient_b: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    reasons: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False,
                                        default="pending", index=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                         nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Front door & master data — user accounts (app.accounts)
# ---------------------------------------------------------------------------

class UserAccount(Base):
    """One managed sign-in account — Front Door pass (app.accounts).

    The account is the IDENTITY record an administrator governs:
    unique username, the person's full name, their sign-in role,
    their unit / service-line assignment, contact details, an optional
    link to their staff-directory entry, and an active/disabled
    status. Passwords are deliberately NOT stored here: production
    sign-in credentials live in the identity provider (AUTH_MODE=oidc,
    Keycloak); accounts govern identity, role, unit and status, in
    both modes.

    In dev mode only, `dev_token_hash` carries the SHA-256 of the
    account's dev bearer token: for the seeded built-in accounts that
    is the hash of the well-known documented token (admin-token etc.,
    so existing sign-ins keep working byte-identically); for an
    account created through the API it is the hash of a generated
    token that is returned to the administrator exactly once, at
    creation, and never retrievable afterwards. security.py resolves
    dev tokens against this table first: a DISABLED account's token
    stops authenticating (401). The column is ignored in OIDC mode.
    `patient_ref` mirrors the dev-token patient link for role=patient
    accounts (portal scoping)."""
    __tablename__ = "user_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    role: Mapped[Role] = mapped_column(Enum(Role), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    # Organisation & Registration Placement pass: governed placement
    # chain (region -> location -> department -> unit codes from the
    # published terminology/locations hierarchy, app.org_hierarchy).
    # Nullable, backfilled by ensure_org_placement where resolvable.
    region: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                               index=True)
    location: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                 index=True)
    department: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                   index=True)
    service_line: Mapped[str | None] = mapped_column(String(120), nullable=True)
    email: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    staff_id: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                 index=True)
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="active", index=True)
    dev_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True,
                                                       index=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Workforce, Teams & Panels pass (app.privileging, app.teams,
# app.onboarding)
# ---------------------------------------------------------------------------

class OnboardingRecord(Base):
    """One employee's onboarding record — Workforce, Teams & Panels
    pass (app.onboarding). Joins the HR record to credentialing /
    privileging and the linked sign-in account: the stage advances
    credentialing -> privileging -> orientation -> account ->
    complete, each step audited. `staff_id` is the staff-directory
    id this record tracks (unique — one onboarding per person).
    Termination is a state on this record plus the directory entry,
    never a delete: the history (who was employed, under which
    credentials, with which privileges) is compliance data."""
    __tablename__ = "onboarding_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    onboarding_id: Mapped[str] = mapped_column(String(120), unique=True,
                                               nullable=False, index=True)
    staff_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    employee_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    stage: Mapped[str] = mapped_column(String(40), nullable=False,
                                       default="credentialing")
    supervisor_staff_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    fte: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    account_username: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    terminated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    termination_reason: Mapped[str] = mapped_column(String(400),
                                                    nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class OrientationCompletion(Base):
    """One orientation checklist item completed for an onboarding
    record — Workforce, Teams & Panels pass (app.onboarding). The
    checklist itself is published `teams` config data
    (orientationItems); this row records that one item was completed,
    by whom and when. One row per (onboarding, item)."""
    __tablename__ = "orientation_completions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    completion_id: Mapped[str] = mapped_column(String(120), unique=True,
                                                nullable=False, index=True)
    onboarding_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                               index=True)
    item_code: Mapped[str] = mapped_column(String(120), nullable=False)
    completed_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                              default="")
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   default=utcnow)
    note: Mapped[str] = mapped_column(String(400), nullable=False, default="")


class StaffPrivilege(Base):
    """One clinical-privilege grant — Workforce, Teams & Panels pass
    (app.privileging). A grant says a staff member may exercise one
    privilege (optionally scoped to one service line) because the
    medical staff office verified the credentials the published
    `privileges` config requires for it. "Verified" is an enforced
    state, not an assertion (Wave A, WF-B1): primary-source
    verification is recorded on CredentialVerification, exclusion
    checks on CredentialExclusionCheck, and
    app.privileging.check_privileges requires the satisfying
    credential's computed verification state to be "verified"
    (current PSV, no exclusion hit, required exclusion kinds clear)
    before a grant satisfies an assignment gate. Grants are never edited:
    revocation is a state change on the row (status, revoked_by,
    revoked_at), so the grant history is itself the appraisal
    record. Assignment gates (app.teams) read ACTIVE grants only."""
    __tablename__ = "staff_privileges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    staff_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    privilege_code: Mapped[str] = mapped_column(String(80), nullable=False)
    service_line: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="active")
    granted_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                           default="")
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    revoked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(400), nullable=False, default="")


class CareTeam(Base):
    """One care team — Workforce, Teams & Panels pass (app.teams).
    A team is a named group of staff (unit-based, service-line,
    consult-service or primary-care, per the published `teams`
    config) that patients are assigned to and that panels and
    coverage hang off. Membership, patient assignment, empanelment
    and coverage are their own rows; this row is the team's identity
    and scope (unit / service line) only."""
    __tablename__ = "care_teams"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    team_type: Mapped[str] = mapped_column(String(60), nullable=False,
                                           default="")
    unit: Mapped[str | None] = mapped_column(String(120), nullable=True)
    service_line: Mapped[str | None] = mapped_column(String(120), nullable=True)
    description: Mapped[str] = mapped_column(String(400), nullable=False,
                                             default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class TeamMembership(Base):
    """One staff member's membership of one care team — Workforce,
    Teams & Panels pass (app.teams). Membership is ended, never
    deleted (status + ended_reason), so who was on a team when is
    always answerable. The `attending` role is privilege-gated at
    join time by app.privileging.check_privileges."""
    __tablename__ = "team_memberships"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    membership_id: Mapped[str] = mapped_column(String(120), unique=True,
                                               nullable=False, index=True)
    team_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    staff_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    role_in_team: Mapped[str] = mapped_column(String(60), nullable=False,
                                              default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="active")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    ended_reason: Mapped[str] = mapped_column(String(400), nullable=False,
                                              default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class TeamPatientAssignment(Base):
    """One team's assignment to one patient — Workforce, Teams &
    Panels pass (app.teams). role=primary is the accountable team;
    role=consulting advises. Lifecycle: requested -> active (accept)
    or declined; active -> ended. A second primary request while one
    is active is a HANDOFF: on accept, the superseded assignment is
    ended in the same transaction (supersedes_assignment_id links
    the two), so a patient never has two accountable teams and never
    has none mid-handoff."""
    __tablename__ = "team_patient_assignments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[str] = mapped_column(String(120), unique=True,
                                               nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)
    encounter_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    team_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(20), nullable=False,
                                       default="requested")
    handoff: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                           default=False)
    supersedes_assignment_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                              default="")
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class Empanelment(Base):
    """One patient's empanelment to a PCP and their primary-care
    team — Workforce, Teams & Panels pass (app.teams, Kaiser
    pattern). Empanelment is supersede-never-edit: re-empanelling a
    patient ends the current active row (ended_reason 'superseded')
    and creates a new one in the same transaction, so panel history
    (who was accountable for this patient, when) is preserved and
    panel sizes are computed from ACTIVE rows only."""
    __tablename__ = "empanelments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    empanelment_id: Mapped[str] = mapped_column(String(120), unique=True,
                                                nullable=False, index=True)
    patient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                             index=True)
    pcp_staff_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                              index=True)
    team_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="active")
    assigned_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    ended_reason: Mapped[str] = mapped_column(String(400), nullable=False,
                                               default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class CoverageDelegation(Base):
    """One coverage delegation — Workforce, Teams & Panels pass
    (app.teams). Exactly one covered side and one covering side,
    each either a staff member or a whole team (XOR enforced at the
    API), over an explicit window: while it is active and the window
    contains now, the covering side sees and works the covered
    side's patients (the team panel merges them, flagged
    via_coverage). Cancellation is a state change, never a delete."""
    __tablename__ = "coverage_delegations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coverage_id: Mapped[str] = mapped_column(String(120), unique=True,
                                             nullable=False, index=True)
    covered_staff_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True, index=True)
    covered_team_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True, index=True)
    covering_staff_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    covering_team_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                              nullable=False)
    reason: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="active")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class PrivacyCase(Base):
    """One privacy-monitoring case — Phase 17, BD-52 (CMP-31,
    app.privacy_monitor).

    Access-pattern analytics over the hash-chained audit open cases
    here when a governed detection rule fires (VIP / coworker /
    family access without a treatment relationship, volume anomaly,
    after-hours pattern). A case is a REVIEW WORK ITEM, not a
    finding of wrongdoing: a privacy officer dispositions it
    (confirmed / dismissed / false_positive) and the disposition is
    hash-chain audited. `dedupe_key` is unique so re-running the
    analysis over the same window never duplicates an open case.
    Evidence carries audit ids and counts only — never chart content.
    """
    __tablename__ = "privacy_cases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    rule: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True,
                                                    index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False,
                                          default="medium")
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="open", index=True)
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True,
                                            nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    opened_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                           default="")
    disposition: Mapped[str | None] = mapped_column(String(40), nullable=True)
    disposition_by: Mapped[str | None] = mapped_column(String(120),
                                                       nullable=True)
    disposition_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    disposition_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)
