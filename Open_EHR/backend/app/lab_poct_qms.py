"""Lab Depth W9 (LAB-D15 + LAB-D16) — POCT enforcement go-live and
laboratory QMS surfaces & teeth.

D15 — POCT enforcement go-live. The Depth-pass POCT programme
(terminology/poct-programme, app.lab) TRACKS device QC + operator
competency and carries two enforcement switches that seed OFF. This
module adds the governed DEPLOYMENT PROFILE (terminology/
poct-deployment-profile, published Studio config): when the profile
publishes `profileMode: "enforce"`, its block flags are enforced AT
POCT RESULT ENTRY by app.lab._create_results (which ORs the profile
over the programme's own switches — a profile can only turn a block
ON, never silently turn a programme-published block off). When the
profile says "track-only" (the seeded default), legacy behaviour is
preserved exactly. This module also owns the per-device lot +
QC-range CONFIGURATION RECORDS (what each device was told: reagent
lot, control lot, control ranges, effective dates — a first-class
record with history) and the per-device / per-operator QC review
surface with corrective-action comments.

HONEST BOUND (D15, stated identically in the API payloads and the
web surface): the PHYSICAL device lockout is an external leg. This
product enforces at its own result-entry surface and records what
each device was configured with; it does NOT reach into device
firmware and does not claim the meter itself refuses an operator.

D16 — laboratory QMS surfaces & teeth.
(a) Read-model surfaces over the four API-only Phase 15 backends in
    app.lab_depth (CLIA competency, lookback, deviation/BPD, EQA
    exchange) — aggregated here for the QMS board; the backends are
    NOT rebuilt.
(b) COMPETENCY GATING (the teeth): a governed per-test mapping
    (terminology/lab-competency-gating) says which CLIA test system
    a catalogue test requires. At result entry (app.lab
    ._create_results, alongside the W4 reagent/instrument lockouts)
    and at verification (app.lab.verify_report, alongside the QC
    gate) the actor's CLIA competency for the mapped test system is
    checked; expired / incomplete / absent competency refuses BY
    NAME with zero writes. Test systems with NO mapping are UNGATED
    — seeded honestly with an empty mapping list, and the gating
    surface SAYS which tests are ungated; nothing is silently gated
    or silently ungated.
(c) Lab NONCONFORMANCE intake: specimen rejections, QC failures,
    corrected reports, PT failures (and audit findings, manual
    reports) each raise a source-linked nonconformance record
    (severity, owner, corrective-action text), mirrored into the
    product's existing safety-event machinery (SafetyEvent,
    category "lab-nonconformance") so it is visible in the product's
    quality/compliance listings. The four-eyes / CLOSURE layer RIDES
    Wave C and is OUT of scope here: intake + assignment + status
    visibility only, labelled as such on every payload.
(d) SOP / procedure library on the Studio document lifecycle
    pattern: versioned procedures (a new version supersedes the
    old), periodic review dates (governed review period), staff
    read-and-acknowledge records per version, overdue reviews
    listed.
(e) Internal-audit programme: scheduled audits with checklist
    templates (governed), tracer records, findings that can raise
    a nonconformance via (c).
(f) PT/EQA failure -> corrective action: a FAILED PT record cannot
    be closed until a linked nonconformance carrying corrective-
    action text exists; closing without one refuses by name.

Studio surfaces (all terminology kind, published, governed):
- terminology/poct-deployment-profile (D15)
- terminology/lab-competency-gating (D16b)
- terminology/lab-qms-programme (D16d/e: SOP review period +
  audit checklist templates)

New tables only (no existing-table columns, so no Run-16 ensure
hook is needed): poct_device_config_records, poct_qc_reviews,
lab_nonconformances, lab_pt_closures, lab_sop_procedures,
lab_sop_acknowledgements, lab_audit_programmes, lab_audit_findings.
Synthetic data only.
"""
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, Integer, JSON, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import lab, lab_depth
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import (ConfigStatus, Device, LabEqaRecord, PoctQcRecord,
                     SafetyEvent, StaffMember, StudioConfig, User, utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/lab/qms", tags=["lab_poct_qms"])

POCT_PROFILE_KEY = "poct-deployment-profile"
GATING_KEY = "lab-competency-gating"
QMS_PROGRAMME_KEY = "lab-qms-programme"

EXTERNAL_LEG_LABEL = (
    "EXTERNAL LEG — physical device lockout is NOT performed by this "
    "product: OpenCare enforces POCT policy at its own result-entry "
    "surface and records what each device was configured with (lots, "
    "control ranges, effective dates). Locking the meter itself is "
    "the device fleet / middleware leg (e.g. the POCT middleware "
    "pushing operator lists to the device) and is NOT RUN here.")

CLOSURE_LABEL = (
    "INTAKE ONLY — nonconformance closure (four-eyes review, "
    "effectiveness check, formal close) rides Wave C and is not "
    "available in this surface; records carry intake, assignment "
    "and status visibility only.")

NC_SOURCE_TYPES = ("specimen-rejection", "qc-failure",
                   "corrected-report", "pt-failure", "audit-finding",
                   "manual")
NC_SEVERITIES = ("minor", "major", "critical")
NC_STATUSES = ("open", "assigned", "in-progress",
               "corrective-action-recorded")


# ---------------------------------------------------------------------------
# Own tables
# ---------------------------------------------------------------------------

class PoctDeviceConfigRecord(Base):
    """One record of what a POCT device was TOLD (D15): reagent lot,
    control lot, per-level control ranges and the effective window.
    History is the table itself — every push is a new row; the
    current record for a device is the latest row whose effective
    window covers today (UTC)."""
    __tablename__ = "poct_device_config_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                            index=True)
    reagent_lot: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    control_lot: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    qc_ranges: Mapped[dict] = mapped_column(JSON, nullable=False,
                                            default=dict)
    effective_from: Mapped[str] = mapped_column(String(20), nullable=False,
                                                default="")
    effective_to: Mapped[str | None] = mapped_column(String(20),
                                                     nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class PoctQcReview(Base):
    """One QC review comment (D15): a reviewer's note on a device's
    or an operator's QC, with corrective-action text when the review
    found a problem."""
    __tablename__ = "poct_qc_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_key: Mapped[str | None] = mapped_column(String(120),
                                                    nullable=True,
                                                    index=True)
    staff_id: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                 index=True)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    corrective_action: Mapped[str] = mapped_column(Text, nullable=False,
                                                   default="")
    reviewer: Mapped[str] = mapped_column(String(120), nullable=False,
                                          default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class LabNonconformance(Base):
    """One lab nonconformance intake record (D16c): source-linked,
    with severity, owner and corrective-action text, mirrored to a
    SafetyEvent (the product's existing quality intake). Closure is
    Wave C — this record never reaches a 'closed' state here."""
    __tablename__ = "lab_nonconformances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nc_id: Mapped[str] = mapped_column(String(120), unique=True,
                                       nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False,
                                             index=True)
    source_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                            default="")
    title: Mapped[str] = mapped_column(String(300), nullable=False,
                                       default="")
    description: Mapped[str] = mapped_column(Text, nullable=False,
                                             default="")
    severity: Mapped[str] = mapped_column(String(20), nullable=False,
                                          default="major")
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    corrective_action: Mapped[str] = mapped_column(Text, nullable=False,
                                                   default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="open")
    safety_event_id: Mapped[str | None] = mapped_column(String(120),
                                                         nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LabPtClosure(Base):
    """Closure of one PT/EQA record (D16f). A failed record may only
    be closed with a linked nonconformance that carries corrective-
    action text."""
    __tablename__ = "lab_pt_closures"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    eqa_record_id: Mapped[int] = mapped_column(Integer, unique=True,
                                               nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(200), nullable=False,
                                       default="")
    passed: Mapped[bool] = mapped_column(default=False, nullable=False)
    nonconformance_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    closed_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                           default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class LabSopProcedure(Base):
    """One VERSION of one laboratory procedure (D16d). A new version
    supersedes the previous current version of the same sop_code;
    history is the table. Review due is effective_date + the
    governed review period unless a review_due_date is recorded."""
    __tablename__ = "lab_sop_procedures"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sop_id: Mapped[str] = mapped_column(String(120), unique=True,
                                        nullable=False, index=True)
    sop_code: Mapped[str] = mapped_column(String(80), nullable=False,
                                          index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False,
                                       default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False,
                                         default=1)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    effective_date: Mapped[str] = mapped_column(String(20), nullable=False,
                                                default="")
    review_due_date: Mapped[str] = mapped_column(String(20), nullable=False,
                                                default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="current")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class LabSopAcknowledgement(Base):
    """One staff read-and-acknowledge record: who acknowledged which
    SOP version, when (D16d)."""
    __tablename__ = "lab_sop_acknowledgements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sop_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                        index=True)
    sop_code: Mapped[str] = mapped_column(String(80), nullable=False,
                                          default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False,
                                         default=1)
    staff_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    acknowledged_by: Mapped[str] = mapped_column(String(120),
                                                 nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class LabAuditProgramme(Base):
    """One scheduled internal audit (D16e): checklist template,
    scheduled date, auditor, tracer scope."""
    __tablename__ = "lab_audit_programmes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[str] = mapped_column(String(120), unique=True,
                                          nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False,
                                       default="")
    checklist_template: Mapped[str] = mapped_column(String(80),
                                                    nullable=False,
                                                    default="")
    checklist_items: Mapped[list] = mapped_column(JSON, nullable=False,
                                                  default=list)
    scheduled_for: Mapped[str] = mapped_column(String(20), nullable=False,
                                               default="")
    auditor: Mapped[str] = mapped_column(String(120), nullable=False,
                                         default="")
    tracer_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                            default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="scheduled")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class LabAuditFinding(Base):
    """One finding from one internal audit (D16e); may raise a
    nonconformance via D16c (nonconformance_id set)."""
    __tablename__ = "lab_audit_findings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    finding: Mapped[str] = mapped_column(Text, nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(20), nullable=False,
                                          default="major")
    tracer_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                            default="")
    nonconformance_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Governed config: defaults, seeds, accessors
# ---------------------------------------------------------------------------

def default_poct_profile_payload() -> dict:
    """D15 deployment profile (terminology/poct-deployment-profile).
    Seeded TRACK-ONLY so go-live changes nothing until a site
    publishes profileMode "enforce" — the auto-verify / POCT
    programme precedent. Under "enforce", a block flag set true here
    is enforced at POCT result entry IN ADDITION TO the programme's
    own switches (app.lab._poct_effective_enforcement ORs them; the
    profile can never weaken a programme-published block)."""
    return {
        "name": "OpenCare POCT deployment profile",
        "concepts": [
            {"code": "poct-programme", "display": "OpenCare POCT programme",
             "active": True},
        ],
        "profileMode": "track-only",
        "blockUncompetentOperator": True,
        "blockMissingDailyQc": True,
        "externalLeg": EXTERNAL_LEG_LABEL,
        "basis": ("Seeded track-only: publishing profileMode 'enforce' "
                  "turns the block flags above into enforced policy at "
                  "POCT result entry. Track-only preserves the legacy "
                  "programme behaviour exactly."),
    }


def default_gating_payload() -> dict:
    """D16(b) competency gating map (terminology/lab-competency-
    gating). Seeded with NO mappings — honestly: every test system
    starts UNGATED, and the gating surface lists it as such. A site
    maps a catalogue test code to the CLIA test system whose current
    six-element competency the entering / verifying actor must hold.
    """
    return {
        "name": "OpenCare laboratory competency gating map",
        "concepts": [
            {"code": "competency-gating",
             "display": "Competency gating map", "active": True},
        ],
        "mappings": [],
        "honesty": ("A catalogue test with no mapping is UNGATED: "
                    "result entry and verification do not check CLIA "
                    "competency for it, and the QMS gating surface "
                    "says so per test. Mapping a test turns the gate "
                    "on for that test only."),
    }


def default_qms_programme_payload() -> dict:
    """D16(d)/(e) QMS programme config (terminology/lab-qms-
    programme): the SOP periodic-review period and the internal-
    audit checklist templates."""
    return {
        "name": "OpenCare laboratory QMS programme",
        "concepts": [
            {"code": "lab-qms", "display": "Laboratory quality "
             "management system", "active": True},
        ],
        "sopReviewPeriodMonths": 12,
        "auditChecklistTemplates": [
            {"code": "general-lab-tracer",
             "display": "General laboratory tracer",
             "items": [
                 "Specimen labelling matches two identifiers at receipt",
                 "QC records complete for the tracer period",
                 "Critical-value read-back documented",
                 "Reagent lots in use are released and in date",
                 "Competency records current for testing personnel",
             ]},
            {"code": "poct-site-tracer",
             "display": "POCT site tracer",
             "items": [
                 "Daily QC recorded for every device in use",
                 "Operators on the device hold current competency",
                 "Device configuration record matches the device lots",
             ]},
        ],
        "closureNote": CLOSURE_LABEL,
    }


def _seed_published(session: Session, kind: str, key: str,
                    payload: dict) -> None:
    if list_config_versions(session, kind, key):
        return
    row = StudioConfig(kind=kind, key=key, version=1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:{kind}/{key}@v1",
                     payload={"kind": kind, "key": key, "version": 1,
                              "seeded_default": True})


def ensure_lab_poct_qms_defaults(session: Session) -> None:
    _seed_published(session, "terminology", POCT_PROFILE_KEY,
                    default_poct_profile_payload())
    _seed_published(session, "terminology", GATING_KEY,
                    default_gating_payload())
    _seed_published(session, "terminology", QMS_PROGRAMME_KEY,
                    default_qms_programme_payload())
    session.commit()


def poct_deployment_profile(session: Session) -> dict:
    cfg = published_config(session, "terminology", POCT_PROFILE_KEY)
    return (cfg.payload or {}) if cfg is not None \
        else default_poct_profile_payload()


def _gating_config(session: Session) -> dict:
    cfg = published_config(session, "terminology", GATING_KEY)
    return (cfg.payload or {}) if cfg is not None \
        else default_gating_payload()


def _qms_programme(session: Session) -> dict:
    cfg = published_config(session, "terminology", QMS_PROGRAMME_KEY)
    return (cfg.payload or {}) if cfg is not None \
        else default_qms_programme_payload()


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422,
                            detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422,
                            detail="Request body must be a JSON object")
    return body


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# D16(b) — competency gating (called by app.lab at entry + verify)
# ---------------------------------------------------------------------------

def _actor_staff_ids(session: Session, user: User) -> set[str]:
    """Every staff-directory identity the actor resolves to: their
    username, their Front Door account's staff id (app.lab's
    operator identities), and staff rows linked by username
    (app.lab_depth's linkage)."""
    ids = set(lab._operator_ids(session, user))
    for staff in lab_depth._linked_staff(session, user):
        ids.add(staff.staff_id)
    return ids


def gating_mappings(session: Session) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for entry in _gating_config(session).get("mappings") or []:
        if isinstance(entry, dict) and entry.get("testCode"):
            out[str(entry["testCode"])] = entry
    return out


def competency_gate_failures(session: Session, user: User,
                             test_codes, stage: str) -> list[str]:
    """Named competency-gate failures for the actor over the given
    catalogue test codes. Only MAPPED tests gate (see the config's
    honesty note); a mapped test fails when the actor holds no CLIA
    competency record for the required test system, or the record's
    computed status is not 'current' (expired / incomplete /
    expiring counts as not current only when expired or incomplete —
    'expiring' is still current until its expiry date)."""
    mappings = gating_mappings(session)
    if not mappings:
        return []
    staff_ids = _actor_staff_ids(session, user)
    config = lab_depth._competency_config(session)
    failures = []
    for code in dict.fromkeys(c for c in test_codes if c):
        entry = mappings.get(str(code))
        if entry is None:
            continue  # unmapped -> ungated, honestly (surface says so)
        test_system = str(entry.get("testSystem") or code)
        record = None
        if staff_ids:
            record = session.scalars(
                select(lab_depth.CliaCompetencyRecord).where(
                    lab_depth.CliaCompetencyRecord.staff_id.in_(
                        sorted(staff_ids)),
                    lab_depth.CliaCompetencyRecord.test_system
                    == test_system)
                .order_by(lab_depth.CliaCompetencyRecord.id.desc())).first()
        if record is None:
            status = "absent"
        else:
            status = lab_depth._competency_out(
                session, config, record)["status"]
        if status not in ("current", "expiring"):
            failures.append(
                f"Competency gate ({stage}): operator '{user.username}' "
                f"holds no current CLIA competency for test system "
                f"'{test_system}' (status: {status}), which the "
                f"published competency gating map "
                f"(terminology/{GATING_KEY}) requires for test "
                f"'{code}'; record a current six-element competency "
                f"assessment for the operator before {stage}")
    return failures


def enforce_competency_gate(session: Session, user: User, test_codes,
                            stage: str) -> None:
    """The D16(b) gate, composed by app.lab alongside the W4
    lockouts at result entry and the QC gate at verification.
    Refuses 409 BY NAME before any write; never weakens any other
    gate (it only adds refusals for mapped tests)."""
    failures = competency_gate_failures(session, user, test_codes, stage)
    if failures:
        raise HTTPException(status_code=409, detail="; ".join(failures))


# ---------------------------------------------------------------------------
# D15 — POCT deployment profile + device configuration records + QC review
# ---------------------------------------------------------------------------

def _device_config_out(row: PoctDeviceConfigRecord,
                       current: bool) -> dict:
    return {"id": row.id, "device_key": row.device_key,
            "reagent_lot": row.reagent_lot,
            "control_lot": row.control_lot,
            "qc_ranges": row.qc_ranges or {},
            "effective_from": row.effective_from,
            "effective_to": row.effective_to,
            "note": row.note, "recorded_by": row.recorded_by,
            "recorded_at": row.created_at.isoformat()
            if row.created_at else None,
            "current": current}


def _current_config_id(session: Session, device_key: str) -> int | None:
    today = _today().isoformat()
    rows = session.scalars(
        select(PoctDeviceConfigRecord).where(
            PoctDeviceConfigRecord.device_key == device_key)
        .order_by(PoctDeviceConfigRecord.id.desc())).all()
    for row in rows:
        if row.effective_from and row.effective_from > today:
            continue
        if row.effective_to and row.effective_to < today:
            continue
        return row.id
    return None


@router.get("/poct/profile")
def poct_profile(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.read"))):
    """The D15 deployment profile + the EFFECTIVE enforcement state
    at POCT result entry (programme switches OR profile enforce
    flags, computed by app.lab._poct_effective_enforcement) + the
    honest external-leg bound."""
    programme = lab._poct_programme(session)
    profile = poct_deployment_profile(session)
    effective = lab._poct_effective_enforcement(session, programme)
    return {
        "profile": profile,
        "profile_config": {"kind": "terminology", "key": POCT_PROFILE_KEY,
                           "edit_in": ("System Studio -> Terminology -> "
                                       "poct-deployment-profile")},
        "programme_enforcement": programme.get("enforcement") or {},
        "profile_mode": profile.get("profileMode") or "track-only",
        "effective_enforcement": {
            "block_uncompetent_operator":
                effective.get("blockUncompetentOperator") is True,
            "block_missing_daily_qc":
                effective.get("blockMissingDailyQc") is True,
        },
        "enforced_at": "POCT result entry (app.lab._create_results)",
        "external_leg": EXTERNAL_LEG_LABEL,
    }


@router.post("/poct/device-config", status_code=201)
async def record_device_config(request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(
                                   require_capability("lab.work"))):
    """Record what a POCT device was told (D15): reagent lot, control
    lot, per-level QC control ranges and the effective window. The
    device must exist in the Phase 14 registry. This is the record
    of the configuration push — the physical push to the device is
    the external leg (see the profile's external_leg label)."""
    payload = await _json_body(request)
    device_key = payload.get("device_key")
    if not isinstance(device_key, str) or not device_key.strip():
        raise HTTPException(status_code=422,
                            detail="'device_key' is required")
    device = session.scalars(select(Device).where(
        Device.device_key == device_key.strip())).first()
    if device is None:
        raise HTTPException(
            status_code=404,
            detail=(f"Unknown device '{device_key}' — POCT devices "
                    "live in the Phase 14 device registry"))
    reagent_lot = payload.get("reagent_lot")
    if not isinstance(reagent_lot, str) or not reagent_lot.strip():
        raise HTTPException(status_code=422,
                            detail="'reagent_lot' is required (the lot "
                                   "the device was configured with)")
    qc_ranges = payload.get("qc_ranges")
    if not isinstance(qc_ranges, dict) or not qc_ranges:
        raise HTTPException(
            status_code=422,
            detail="'qc_ranges' must be a non-empty object keyed by "
                   "QC level, each {low, high}")
    cleaned_ranges: dict[str, dict] = {}
    for level, rng in qc_ranges.items():
        if not isinstance(rng, dict):
            raise HTTPException(
                status_code=422,
                detail=f"qc_ranges['{level}'] must be an object "
                       "{low, high}")
        try:
            low, high = float(rng.get("low")), float(rng.get("high"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail=f"qc_ranges['{level}'] low/high must be numbers")
        if low > high:
            raise HTTPException(
                status_code=422,
                detail=f"qc_ranges['{level}'] low must not exceed high")
        cleaned_ranges[str(level)] = {"low": low, "high": high}
    effective_from = payload.get("effective_from") or _today().isoformat()
    try:
        date.fromisoformat(str(effective_from))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail="'effective_from' must be an ISO date")
    effective_to = payload.get("effective_to")
    if effective_to is not None:
        try:
            date.fromisoformat(str(effective_to))
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="'effective_to' must be an ISO date")
        if str(effective_to) < str(effective_from):
            raise HTTPException(
                status_code=422,
                detail="'effective_to' must not precede 'effective_from'")
    row = PoctDeviceConfigRecord(
        device_key=device.device_key, reagent_lot=reagent_lot.strip(),
        control_lot=str(payload.get("control_lot") or "").strip(),
        qc_ranges=cleaned_ranges, effective_from=str(effective_from),
        effective_to=str(effective_to) if effective_to else None,
        note=str(payload.get("note") or ""), recorded_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.poct.device_config",
                 entity=f"Device/{device.device_key}",
                 payload={"reagent_lot": row.reagent_lot,
                          "control_lot": row.control_lot,
                          "effective_from": row.effective_from,
                          "external_leg": "record-only; physical push "
                                          "is the device-fleet leg"})
    session.commit()
    return {"config": _device_config_out(
                row, _current_config_id(session, row.device_key) == row.id),
            "external_leg": EXTERNAL_LEG_LABEL}


@router.get("/poct/device-config")
def list_device_config(device_key: str | None = Query(default=None),
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.read"))):
    """Configuration history per device (newest first), each row
    flagged current when its effective window covers today."""
    stmt = select(PoctDeviceConfigRecord).order_by(
        PoctDeviceConfigRecord.id.desc())
    if device_key:
        stmt = stmt.where(
            PoctDeviceConfigRecord.device_key == device_key)
    rows = session.scalars(stmt).all()
    current_ids: dict[str, int | None] = {}
    items = []
    for row in rows:
        if row.device_key not in current_ids:
            current_ids[row.device_key] = _current_config_id(
                session, row.device_key)
        items.append(_device_config_out(
            row, current_ids[row.device_key] == row.id))
    return {"items": items, "total": len(items),
            "external_leg": EXTERNAL_LEG_LABEL}


@router.get("/poct/qc-review")
def poct_qc_review(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    """Per-device and per-operator QC review surface data (D15):
    today's pass/fail per required level per device, each operator's
    recorded QC counts, the current configuration record per device,
    and the review comments (with corrective actions) on file."""
    programme = lab._poct_programme(session)
    levels = [lvl for lvl in (programme.get("qcLevels") or [])
              if isinstance(lvl, str)]
    today = _today()
    rows = session.scalars(select(PoctQcRecord).order_by(
        PoctQcRecord.id.desc())).all()
    by_device: dict[str, dict] = {}
    by_operator: dict[str, dict] = {}
    for row in rows:
        dev = by_device.setdefault(row.device_key, {
            "device_key": row.device_key, "qc_current": None,
            "levels_today": {lvl: None for lvl in levels},
            "runs_total": 0, "fails_total": 0})
        dev["runs_total"] += 1
        if row.result == "fail":
            dev["fails_total"] += 1
        if row.created_at is not None and row.created_at.date() == today \
                and row.level in dev["levels_today"] \
                and dev["levels_today"][row.level] is None:
            dev["levels_today"][row.level] = row.result
        op = by_operator.setdefault(row.operator, {
            "operator": row.operator, "runs": 0, "passes": 0,
            "fails": 0, "devices": set()})
        op["runs"] += 1
        op["passes" if row.result == "pass" else "fails"] += 1
        op["devices"].add(row.device_key)
    devices_out = []
    for device_key in sorted(by_device):
        dev = by_device[device_key]
        dev["qc_current"] = lab._poct_qc_current(session, programme,
                                                 device_key)
        current_id = _current_config_id(session, device_key)
        dev["current_config"] = None
        if current_id is not None:
            cfg_row = session.get(PoctDeviceConfigRecord, current_id)
            if cfg_row is not None:
                dev["current_config"] = _device_config_out(cfg_row, True)
        devices_out.append(dev)
    operators_out = []
    for operator in sorted(by_operator):
        op = by_operator[operator]
        op = dict(op, devices=sorted(op["devices"]))
        operators_out.append(op)
    reviews = session.scalars(select(PoctQcReview).order_by(
        PoctQcReview.id.desc()).limit(200)).all()
    return {
        "devices": devices_out, "operators": operators_out,
        "reviews": [{"id": r.id, "device_key": r.device_key,
                     "staff_id": r.staff_id, "comment": r.comment,
                     "corrective_action": r.corrective_action,
                     "reviewer": r.reviewer,
                     "recorded_at": r.created_at.isoformat()
                     if r.created_at else None} for r in reviews],
        "external_leg": EXTERNAL_LEG_LABEL,
    }


@router.post("/poct/qc-review", status_code=201)
async def add_qc_review(request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    """Record a QC review comment for a device and/or an operator,
    with corrective-action text when the review found a problem."""
    payload = await _json_body(request)
    device_key = payload.get("device_key")
    staff_id = payload.get("staff_id")
    if not device_key and not staff_id:
        raise HTTPException(
            status_code=422,
            detail="'device_key' and/or 'staff_id' is required (the "
                   "review's subject)")
    if device_key:
        device = session.scalars(select(Device).where(
            Device.device_key == str(device_key))).first()
        if device is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown device '{device_key}' — POCT devices "
                       "live in the Phase 14 device registry")
    comment = payload.get("comment")
    if not isinstance(comment, str) or not comment.strip():
        raise HTTPException(status_code=422,
                            detail="'comment' is required")
    row = PoctQcReview(
        device_key=str(device_key) if device_key else None,
        staff_id=str(staff_id) if staff_id else None,
        comment=comment.strip(),
        corrective_action=str(payload.get("corrective_action") or ""),
        reviewer=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="lab.poct.qc_review",
                 entity=(f"Device/{row.device_key}" if row.device_key
                         else f"Staff/{row.staff_id}"),
                 payload={"comment": row.comment,
                          "corrective_action": row.corrective_action})
    session.commit()
    return {"review": {"id": row.id, "device_key": row.device_key,
                       "staff_id": row.staff_id, "comment": row.comment,
                       "corrective_action": row.corrective_action,
                       "reviewer": row.reviewer}}


# ---------------------------------------------------------------------------
# D16(a) — surfaces over the four API-only backends (app.lab_depth)
# ---------------------------------------------------------------------------

@router.get("/competency/board")
def competency_board(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.read"))):
    """CLIA competency board: every per-person per-test-system record
    with computed currency (app.lab_depth read model), the worklist
    summary, and the gating map's per-test gated/ungated honesty."""
    records = lab_depth.list_competency(staff_id=None, session=session,
                                        user=user)
    worklist = lab_depth.competency_worklist(session=session, user=user)
    return {"records": records["items"], "total": records["total"],
            "worklist": {"total": worklist["total"],
                         "expired": worklist["expired"],
                         "incomplete": worklist["incomplete"],
                         "worklist_window_days":
                             worklist["worklist_window_days"]},
            "config": {"kind": "terminology",
                       "key": lab_depth.COMPETENCY_KEY}}


@router.get("/competency/gating")
def competency_gating(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    """The gating map, honestly: every catalogue test listed as
    gated (with the CLIA test system it requires) or UNGATED."""
    mappings = gating_mappings(session)
    catalog = lab._catalog(session)
    tests = []
    for concept in catalog.get("concepts") or []:
        if not isinstance(concept, dict) or not concept.get("code"):
            continue
        entry = mappings.get(str(concept["code"]))
        tests.append({
            "code": concept["code"],
            "display": concept.get("display"),
            "gated": entry is not None,
            "required_test_system": (str(entry.get("testSystem"))
                                     if entry else None),
            "state_label": (
                f"GATED — current CLIA competency for "
                f"'{entry.get('testSystem')}' required at result entry "
                f"and verification" if entry else
                "UNGATED — no competency mapping published for this "
                "test; entry and verification do not check competency"),
        })
    return {"mappings": [mappings[k] for k in sorted(mappings)],
            "tests": tests,
            "gated_count": sum(1 for t in tests if t["gated"]),
            "ungated_count": sum(1 for t in tests if not t["gated"]),
            "config": {"kind": "terminology", "key": GATING_KEY,
                       "edit_in": ("System Studio -> Terminology -> "
                                   "lab-competency-gating")}}


@router.get("/lookback")
def qms_lookback(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.read"))):
    return lab_depth.list_lookbacks(session=session, user=user)


@router.get("/deviations")
def qms_deviations(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    return lab_depth.list_deviations(session=session, user=user)


@router.get("/eqa")
def qms_eqa(session: Session = Depends(get_db),
            user: User = Depends(require_capability("lab.read"))):
    """EQA exchange panel data: enrolments (app.lab_depth), the
    fixture-defined provider basis, and PT records with their
    D16(f) closure state."""
    enrolments = lab_depth.list_enrolments(session=session, user=user)
    return {"enrolments": enrolments["items"],
            "total": enrolments["total"],
            "provider": lab_depth._eqa_provider(session),
            "pt_events": _pt_events(session),
            "live_exchange": "NOT RUN — fixture-defined format only"}


# ---------------------------------------------------------------------------
# D16(c) — lab nonconformance intake (+ D16(f) PT corrective action)
# ---------------------------------------------------------------------------

def _nc_out(row: LabNonconformance) -> dict:
    return {"nc_id": row.nc_id, "source_type": row.source_type,
            "source_ref": row.source_ref, "title": row.title,
            "description": row.description, "severity": row.severity,
            "owner": row.owner,
            "corrective_action": row.corrective_action,
            "status": row.status,
            "safety_event_id": row.safety_event_id,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat()
            if row.created_at else None,
            "closure_state": "closure-pending-wave-c",
            "closure_label": CLOSURE_LABEL}


def raise_nonconformance(session: Session, user: User, *,
                         source_type: str, source_ref: str,
                         title: str, description: str = "",
                         severity: str = "major",
                         owner: str | None = None,
                         corrective_action: str = "") -> LabNonconformance:
    """File one nonconformance (D16c) and mirror it into the
    product's existing safety-event machinery (SafetyEvent category
    'lab-nonconformance') so the record is visible in the product's
    quality/compliance listings. Caller validates + commits."""
    nc_id = f"lab-nc-{uuid.uuid4().hex[:10]}"
    event = SafetyEvent(
        event_id=f"sev-{uuid.uuid4().hex[:10]}", reporter=user.username,
        category="lab-nonconformance",
        harm_level={"minor": "none", "major": "low",
                    "critical": "moderate"}.get(severity, "low"),
        description=(f"[{source_type}] {title} — source: {source_ref}"
                     + (f" — {description}" if description else "")),
        status="reported")
    session.add(event)
    session.flush()
    row = LabNonconformance(
        nc_id=nc_id, source_type=source_type, source_ref=source_ref,
        title=title, description=description, severity=severity,
        owner=owner, corrective_action=corrective_action,
        status="assigned" if owner else "open",
        safety_event_id=event.event_id, created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.nonconformance.raise",
                 entity=f"LabNonconformance/{nc_id}",
                 payload={"source_type": source_type,
                          "source_ref": source_ref,
                          "severity": severity,
                          "safety_event_id": event.event_id,
                          "closure": "rides Wave C (intake only)"})
    return row


@router.post("/nonconformances", status_code=201)
async def create_nonconformance(request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(
                                    require_capability("lab.work"))):
    """Raise a lab nonconformance from one of the four lab failure
    signals — specimen rejection, QC failure, corrected report, PT
    failure — an audit finding, or a manual report. Intake +
    assignment + status only; closure rides Wave C (labelled)."""
    payload = await _json_body(request)
    source_type = payload.get("source_type")
    if source_type not in NC_SOURCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"'source_type' must be one of {list(NC_SOURCE_TYPES)}")
    source_ref = payload.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise HTTPException(
            status_code=422,
            detail="'source_ref' is required (the order / QC run / "
                   "report / PT record this nonconformance links to)")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=422,
                            detail="'title' is required")
    severity = payload.get("severity") or "major"
    if severity not in NC_SEVERITIES:
        raise HTTPException(
            status_code=422,
            detail=f"'severity' must be one of {list(NC_SEVERITIES)}")
    owner = payload.get("owner")
    if owner is not None and not isinstance(owner, str):
        raise HTTPException(status_code=422,
                            detail="'owner' must be a staff id or username")
    row = raise_nonconformance(
        session, user, source_type=source_type,
        source_ref=source_ref.strip(), title=title.strip(),
        description=str(payload.get("description") or ""),
        severity=severity,
        owner=owner.strip() if isinstance(owner, str) and owner.strip()
        else None,
        corrective_action=str(payload.get("corrective_action") or ""))
    session.commit()
    return {"nonconformance": _nc_out(row)}


@router.get("/nonconformances")
def list_nonconformances(
        source_type: str | None = Query(default=None),
        status: str | None = Query(default=None),
        session: Session = Depends(get_db),
        user: User = Depends(require_capability("lab.read"))):
    stmt = select(LabNonconformance).order_by(LabNonconformance.id.desc())
    if source_type:
        stmt = stmt.where(LabNonconformance.source_type == source_type)
    if status:
        stmt = stmt.where(LabNonconformance.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_nc_out(r) for r in rows], "total": len(rows),
            "closure_label": CLOSURE_LABEL}


@router.post("/nonconformances/{nc_id}/assign")
async def assign_nonconformance(nc_id: str, request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(
                                    require_capability("lab.work"))):
    """Assign an owner and/or move the intake status. 'closed' is
    refused BY NAME: closure (four-eyes review + effectiveness
    check) rides Wave C and is not part of this intake surface."""
    payload = await _json_body(request)
    row = session.scalars(select(LabNonconformance).where(
        LabNonconformance.nc_id == nc_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Nonconformance '{nc_id}' not found")
    new_status = payload.get("status")
    if new_status is not None:
        if new_status == "closed":
            raise HTTPException(
                status_code=422,
                detail=("Nonconformance closure rides Wave C "
                        "(four-eyes review + effectiveness check) and "
                        "cannot be recorded on this intake surface"))
        if new_status not in NC_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"'status' must be one of {list(NC_STATUSES)}")
        row.status = new_status
    owner = payload.get("owner")
    if isinstance(owner, str) and owner.strip():
        row.owner = owner.strip()
        if row.status == "open":
            row.status = "assigned"
    corrective = payload.get("corrective_action")
    if isinstance(corrective, str) and corrective.strip():
        row.corrective_action = corrective.strip()
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.nonconformance.assign",
                 entity=f"LabNonconformance/{nc_id}",
                 payload={"owner": row.owner, "status": row.status})
    session.commit()
    return {"nonconformance": _nc_out(row)}


def _pt_events(session: Session) -> list[dict]:
    closures = {c.eqa_record_id: c for c in
                session.scalars(select(LabPtClosure)).all()}
    ncs = session.scalars(select(LabNonconformance).where(
        LabNonconformance.source_type == "pt-failure")).all()
    records = session.scalars(select(LabEqaRecord).order_by(
        LabEqaRecord.id.desc())).all()
    out = []
    for record in records:
        linked = [n for n in ncs
                  if n.source_ref in (str(record.id), record.event)
                  and n.corrective_action.strip()]
        closure = closures.get(record.id)
        out.append({
            "record_id": record.id, "event": record.event,
            "analyte_code": record.analyte_code,
            "reported_value": record.reported_value,
            "acceptable_low": record.acceptable_low,
            "acceptable_high": record.acceptable_high,
            "passed": bool(record.passed),
            "recorded_by": record.recorded_by,
            "closure": ({"closed_by": closure.closed_by,
                         "closed_at": closure.created_at.isoformat()
                         if closure.created_at else None,
                         "nonconformance_id": closure.nonconformance_id}
                        if closure else None),
            "corrective_action_nc": linked[0].nc_id if linked else None,
            "closeable": bool(record.passed) or bool(linked),
        })
    return out


@router.get("/pt-events")
def pt_events(session: Session = Depends(get_db),
              user: User = Depends(require_capability("lab.read"))):
    return {"items": _pt_events(session),
            "rule": ("A failed PT event requires an opened corrective "
                     "action (a pt-failure nonconformance with "
                     "corrective-action text) before it can be "
                     "closed (D16f).")}


@router.post("/pt-events/{record_id}/close")
def close_pt_event(record_id: int, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.work"))):
    """Close one PT/EQA record (D16f). A PASSED record closes
    directly. A FAILED record refuses BY NAME until a pt-failure
    nonconformance linked to this record (or its event) carries
    corrective-action text."""
    record = session.get(LabEqaRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404,
                            detail=f"PT record '{record_id}' not found")
    existing = session.scalars(select(LabPtClosure).where(
        LabPtClosure.eqa_record_id == record_id)).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"PT record {record_id} (event '{record.event}') is "
                   f"already closed by '{existing.closed_by}'")
    nc = None
    if not record.passed:
        candidates = session.scalars(select(LabNonconformance).where(
            LabNonconformance.source_type == "pt-failure")).all()
        nc = next((n for n in candidates
                   if n.source_ref in (str(record.id), record.event)
                   and n.corrective_action.strip()), None)
        if nc is None:
            raise HTTPException(
                status_code=409,
                detail=(f"PT failure corrective action required: PT "
                        f"record {record.id} (event '{record.event}', "
                        f"analyte '{record.analyte_code}') FAILED and "
                        "no corrective action is on file — raise a "
                        "pt-failure nonconformance with corrective-"
                        "action text (POST /lab/qms/nonconformances) "
                        "before this event can be closed"))
    closure = LabPtClosure(
        eqa_record_id=record.id, event=record.event,
        passed=bool(record.passed),
        nonconformance_id=nc.nc_id if nc else None,
        closed_by=user.username)
    session.add(closure)
    session.flush()
    append_audit(session, actor=user.username, action="lab.pt.close",
                 entity=f"LabEqaRecord/{record.id}",
                 payload={"event": record.event, "passed": record.passed,
                          "nonconformance_id": closure.nonconformance_id})
    session.commit()
    return {"closure": {"record_id": record.id, "event": record.event,
                        "passed": bool(record.passed),
                        "nonconformance_id": closure.nonconformance_id,
                        "closed_by": user.username}}


# ---------------------------------------------------------------------------
# D16(d) — SOP / procedure library (Studio document lifecycle pattern)
# ---------------------------------------------------------------------------

def _sop_out(session: Session, row: LabSopProcedure) -> dict:
    today = _today().isoformat()
    acks = session.scalars(select(LabSopAcknowledgement).where(
        LabSopAcknowledgement.sop_id == row.sop_id)).all()
    return {"sop_id": row.sop_id, "sop_code": row.sop_code,
            "title": row.title, "version": row.version,
            "body": row.body, "effective_date": row.effective_date,
            "review_due_date": row.review_due_date,
            "status": row.status,
            "review_overdue": bool(row.status == "current"
                                   and row.review_due_date
                                   and row.review_due_date < today),
            "acknowledgements": len(acks),
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat()
            if row.created_at else None}


@router.post("/sops", status_code=201)
async def create_sop(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.work"))):
    """Create a procedure, or a NEW VERSION of an existing sop_code
    (the previous current version is superseded, never edited —
    the Studio document lifecycle pattern). Review due defaults to
    effective_date + the governed review period (terminology/
    lab-qms-programme sopReviewPeriodMonths)."""
    payload = await _json_body(request)
    sop_code = payload.get("sop_code")
    if not isinstance(sop_code, str) or not sop_code.strip():
        raise HTTPException(status_code=422,
                            detail="'sop_code' is required")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=422,
                            detail="'title' is required")
    body_text = payload.get("body")
    if not isinstance(body_text, str) or not body_text.strip():
        raise HTTPException(
            status_code=422,
            detail="'body' is required (the procedure text for this "
                   "version)")
    effective = payload.get("effective_date") or _today().isoformat()
    try:
        effective_date = date.fromisoformat(str(effective))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail="'effective_date' must be an ISO date")
    review_due = payload.get("review_due_date")
    if review_due:
        try:
            date.fromisoformat(str(review_due))
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="'review_due_date' must be an ISO date")
    else:
        months = _qms_programme(session).get("sopReviewPeriodMonths")
        if not isinstance(months, int) or isinstance(months, bool) \
                or months <= 0:
            months = 12
        review_due = lab._add_months(effective_date, months).isoformat()
    previous = session.scalars(select(LabSopProcedure).where(
        LabSopProcedure.sop_code == sop_code.strip(),
        LabSopProcedure.status == "current")).first()
    version = (previous.version + 1) if previous else 1
    if previous is not None:
        previous.status = "superseded"
    row = LabSopProcedure(
        sop_id=f"sop-{uuid.uuid4().hex[:10]}", sop_code=sop_code.strip(),
        title=title.strip(), version=version, body=body_text,
        effective_date=effective_date.isoformat(),
        review_due_date=str(review_due), status="current",
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="lab.sop.create",
                 entity=f"LabSop/{row.sop_id}",
                 payload={"sop_code": row.sop_code,
                          "version": version,
                          "review_due_date": row.review_due_date})
    session.commit()
    return {"sop": _sop_out(session, row)}


@router.get("/sops")
def list_sops(session: Session = Depends(get_db),
              user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(select(LabSopProcedure).order_by(
        LabSopProcedure.sop_code.asc(),
        LabSopProcedure.version.desc())).all()
    items = [_sop_out(session, r) for r in rows]
    return {"items": items, "total": len(items),
            "overdue_reviews": [i for i in items if i["review_overdue"]],
            "config": {"kind": "terminology", "key": QMS_PROGRAMME_KEY}}


@router.post("/sops/{sop_id}/acknowledge", status_code=201)
async def acknowledge_sop(sop_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(
                              require_capability("lab.work"))):
    """Record a staff read-and-acknowledge for THIS SOP version.
    The staff member defaults to the caller's linked staff record;
    an explicit staff_id must exist in the staff directory.
    Acknowledging a superseded version is refused by name."""
    payload = await _json_body(request)
    row = session.scalars(select(LabSopProcedure).where(
        LabSopProcedure.sop_id == sop_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"SOP '{sop_id}' not found")
    if row.status != "current":
        raise HTTPException(
            status_code=409,
            detail=(f"SOP '{row.sop_code}' version {row.version} is "
                    f"{row.status}; acknowledge the current version "
                    "instead"))
    staff_id = payload.get("staff_id")
    if staff_id:
        staff = session.scalars(select(StaffMember).where(
            StaffMember.staff_id == str(staff_id))).first()
        if staff is None:
            raise HTTPException(
                status_code=422,
                detail=f"Staff member '{staff_id}' is not in the "
                       "staff directory")
        staff_id = staff.staff_id
    else:
        linked = lab_depth._linked_staff(session, user)
        if not linked:
            raise HTTPException(
                status_code=422,
                detail=("'staff_id' is required — caller "
                        f"'{user.username}' has no linked staff-"
                        "directory record"))
        staff_id = linked[0].staff_id
    existing = session.scalars(select(LabSopAcknowledgement).where(
        LabSopAcknowledgement.sop_id == sop_id,
        LabSopAcknowledgement.staff_id == staff_id)).first()
    if existing is not None:
        return {"acknowledgement": {
                    "id": existing.id, "sop_id": existing.sop_id,
                    "sop_code": existing.sop_code,
                    "version": existing.version,
                    "staff_id": existing.staff_id,
                    "acknowledged_at": existing.created_at.isoformat()
                    if existing.created_at else None},
                "already_acknowledged": True}
    ack = LabSopAcknowledgement(
        sop_id=row.sop_id, sop_code=row.sop_code, version=row.version,
        staff_id=staff_id, acknowledged_by=user.username)
    session.add(ack)
    session.flush()
    append_audit(session, actor=user.username, action="lab.sop.acknowledge",
                 entity=f"LabSop/{row.sop_id}",
                 payload={"staff_id": staff_id, "version": row.version})
    session.commit()
    return {"acknowledgement": {
                "id": ack.id, "sop_id": ack.sop_id,
                "sop_code": ack.sop_code, "version": ack.version,
                "staff_id": ack.staff_id,
                "acknowledged_at": ack.created_at.isoformat()
                if ack.created_at else None},
            "already_acknowledged": False}


@router.get("/sops/{sop_id}/acknowledgements")
def sop_acknowledgements(sop_id: str, session: Session = Depends(get_db),
                         user: User = Depends(
                             require_capability("lab.read"))):
    rows = session.scalars(select(LabSopAcknowledgement).where(
        LabSopAcknowledgement.sop_id == sop_id).order_by(
        LabSopAcknowledgement.id.asc())).all()
    return {"items": [{"id": r.id, "sop_id": r.sop_id,
                       "sop_code": r.sop_code, "version": r.version,
                       "staff_id": r.staff_id,
                       "acknowledged_by": r.acknowledged_by,
                       "acknowledged_at": r.created_at.isoformat()
                       if r.created_at else None} for r in rows],
            "total": len(rows)}


# ---------------------------------------------------------------------------
# D16(e) — internal-audit programme
# ---------------------------------------------------------------------------

def _audit_out(session: Session, row: LabAuditProgramme) -> dict:
    findings = session.scalars(select(LabAuditFinding).where(
        LabAuditFinding.audit_id == row.audit_id).order_by(
        LabAuditFinding.id.asc())).all()
    return {"audit_id": row.audit_id, "title": row.title,
            "checklist_template": row.checklist_template,
            "checklist_items": row.checklist_items or [],
            "scheduled_for": row.scheduled_for, "auditor": row.auditor,
            "tracer_ref": row.tracer_ref, "status": row.status,
            "created_by": row.created_by,
            "findings": [{"id": f.id, "finding": f.finding,
                          "severity": f.severity,
                          "tracer_ref": f.tracer_ref,
                          "nonconformance_id": f.nonconformance_id,
                          "created_by": f.created_by}
                         for f in findings]}


@router.post("/audits", status_code=201)
async def schedule_audit(request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.work"))):
    """Schedule one internal audit from a governed checklist
    template (terminology/lab-qms-programme) or an explicit
    checklist. Tracer scope is recorded on the audit."""
    payload = await _json_body(request)
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=422,
                            detail="'title' is required")
    templates = {t.get("code"): t for t in
                 _qms_programme(session).get("auditChecklistTemplates")
                 or [] if isinstance(t, dict) and t.get("code")}
    template_code = payload.get("checklist_template") or ""
    items = payload.get("checklist_items")
    if template_code:
        template = templates.get(str(template_code))
        if template is None:
            raise HTTPException(
                status_code=422,
                detail=(f"Checklist template '{template_code}' is not "
                        f"in the published QMS programme "
                        f"{sorted(templates)}"))
        if not isinstance(items, list):
            items = list(template.get("items") or [])
    if not isinstance(items, list) or not items:
        raise HTTPException(
            status_code=422,
            detail="'checklist_items' must be a non-empty list (or "
                   "name a governed 'checklist_template')")
    scheduled_for = payload.get("scheduled_for") or _today().isoformat()
    try:
        date.fromisoformat(str(scheduled_for))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail="'scheduled_for' must be an ISO date")
    auditor = payload.get("auditor")
    if not isinstance(auditor, str) or not auditor.strip():
        raise HTTPException(status_code=422,
                            detail="'auditor' is required")
    row = LabAuditProgramme(
        audit_id=f"lab-audit-{uuid.uuid4().hex[:10]}",
        title=title.strip(), checklist_template=str(template_code),
        checklist_items=[str(i) for i in items],
        scheduled_for=str(scheduled_for), auditor=auditor.strip(),
        tracer_ref=str(payload.get("tracer_ref") or ""),
        status="scheduled", created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="lab.audit.schedule",
                 entity=f"LabAudit/{row.audit_id}",
                 payload={"title": row.title,
                          "scheduled_for": row.scheduled_for})
    session.commit()
    return {"audit": _audit_out(session, row)}


@router.get("/audits")
def list_audits(session: Session = Depends(get_db),
                user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(select(LabAuditProgramme).order_by(
        LabAuditProgramme.id.desc())).all()
    items = [_audit_out(session, r) for r in rows]
    templates = _qms_programme(session).get("auditChecklistTemplates") or []
    return {"items": items, "total": len(items),
            "checklist_templates": templates,
            "config": {"kind": "terminology", "key": QMS_PROGRAMME_KEY}}


@router.post("/audits/{audit_id}/findings", status_code=201)
async def add_audit_finding(audit_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(
                                require_capability("lab.work"))):
    """Record one audit finding (a tracer record). With
    raise_nonconformance: true the finding ALSO raises a linked
    nonconformance via the D16(c) intake."""
    payload = await _json_body(request)
    audit = session.scalars(select(LabAuditProgramme).where(
        LabAuditProgramme.audit_id == audit_id)).first()
    if audit is None:
        raise HTTPException(status_code=404,
                            detail=f"Audit '{audit_id}' not found")
    finding = payload.get("finding")
    if not isinstance(finding, str) or not finding.strip():
        raise HTTPException(status_code=422,
                            detail="'finding' is required")
    severity = payload.get("severity") or "major"
    if severity not in NC_SEVERITIES:
        raise HTTPException(
            status_code=422,
            detail=f"'severity' must be one of {list(NC_SEVERITIES)}")
    tracer_ref = str(payload.get("tracer_ref") or audit.tracer_ref or "")
    row = LabAuditFinding(
        audit_id=audit_id, finding=finding.strip(), severity=severity,
        tracer_ref=tracer_ref, created_by=user.username)
    session.add(row)
    session.flush()
    nc_out = None
    if payload.get("raise_nonconformance") is True:
        nc = raise_nonconformance(
            session, user, source_type="audit-finding",
            source_ref=audit_id, title=finding.strip()[:300],
            description=(f"Internal audit '{audit.title}' finding; "
                         f"tracer: {tracer_ref or '—'}"),
            severity=severity,
            owner=str(payload.get("owner") or "") or None,
            corrective_action=str(payload.get("corrective_action")
                                  or ""))
        row.nonconformance_id = nc.nc_id
        nc_out = _nc_out(nc)
    if audit.status == "scheduled":
        audit.status = "in-progress"
    session.flush()
    append_audit(session, actor=user.username, action="lab.audit.finding",
                 entity=f"LabAudit/{audit_id}",
                 payload={"severity": severity,
                          "nonconformance_id": row.nonconformance_id})
    session.commit()
    return {"finding": {"id": row.id, "audit_id": audit_id,
                        "finding": row.finding, "severity": severity,
                        "tracer_ref": tracer_ref,
                        "nonconformance_id": row.nonconformance_id},
            "nonconformance": nc_out}
