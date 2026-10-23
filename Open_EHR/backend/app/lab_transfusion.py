"""LAB-D13 — Transfusion service completion (Lab Depth pass, W7).

The depth layer over Wave A's landed blood-bank spine
(app.lab_benches): Wave A owns the unit inventory, the HARD issue
gate (_evaluate_issue_gate), BCMA bedside verification, and reaction
CONTAINMENT (quarantine + patient hold). This module owns everything
around that spine that a real transfusion service runs, and feeds
the gate through the gate's own composition — it never forks it:

- T&S SPECIMEN-VALIDITY LIFECYCLE: a type-and-screen specimen record
  (models.LabTsSpecimen) carries its collection time and a computed
  expiry: the governed STANDARD window, or the governed EXTENDED
  presurgical window only when the governed eligibility holds (no
  transfusion and no pregnancy in the governed lookback window —
  caller attestation checked against the patient's transfusion
  Procedures on file). The Wave A gate's screen-freshness read
  consults this record (see lab_benches._evaluate_issue_gate, check
  `antibody_screen`): an expired specimen fails by name, an
  in-window extended specimen passes on the lifecycle window.
  With NO specimen record the gate behaves exactly as Wave A left
  it (catalogue `bloodBankGate.antibodyScreenValidityHours`).
- ANTIBODY IDENTIFICATION + HISTORICAL ANTIBODY RECORD: panel
  workups (method, panel reactions, identified specificity, ruled
  out) and a PERMANENT historical record per patient
  (models.LabHistoricalAntibody). The gate consults that record:
  a historical clinically-significant antibody (a) removes
  electronic-crossmatch eligibility and (b) requires the unit to
  carry an attested antigen-negative attribute for the specificity
  (gate check `historical_antibody`, contradictory class — an
  emergency release cannot override it).
- SPECIAL REQUIREMENTS: standing patient requirements (irradiated,
  CMV-negative, washed, antigen-negative) satisfied per unit by
  attested unit attributes; the gate names every unmet requirement
  (check `special_requirements`, contradictory class).
- RESERVATION ON CROSSMATCH: a Compatible crossmatch reserves the
  unit for that patient (lab_benches.result_crossmatch creates the
  reservation; this module also exposes explicit reserve / release).
  An active reservation for another patient refuses crossmatch,
  reservation and issue by name (gate check `unit_reservation`).
  Release paths: issue (released in the issue transaction),
  expiry (governed hours, computed at read), manual release with a
  reason — all audited.
- THAW / PREPARATION: a governed step sequence per prep kind with
  recorded times; on completion the unit carries a post-thaw expiry
  (governed hours per product) that the gate enforces by name
  (check `preparation_expiry`, contradictory class).
- WASTAGE / DISCARD ACCOUNTING: every unit's terminal disposition
  (transfused / wasted / discarded / returned / expired) recorded
  with a governed reason code; a wastage report aggregates the
  wasted + discarded + expired dispositions by reason and period.
- REACTION WORKUP: built ON Wave A containment (the unit must
  already be quarantined by the reaction endpoint — containment
  stays first). A FIXED sequence (clinical standard, in code, not
  configuration — the Wave A matrix precedent): clerical check ->
  visual haemolysis inspection -> DAT -> repeat ABO/screen/
  crossmatch -> cultures/coag where indicated; steps complete
  strictly in order, each with result + actor; then severity +
  imputability grading; then a haemovigilance report artefact.

Haemovigilance filing — honest label: the report is a NEW record
(report_id `hv-...`) filed into the EXISTING biological-product-
deviation backend (app.lab_depth.DeviationReport /
lab_deviation_reports, with its 21 CFR 606.160 retention block and
its PDF machinery). It is not a lookback case; a lookback is opened
separately through the existing /lab/blood-bank/lookback endpoint
when the workup indicates one.

Configure, don't fork — governed Studio configuration
(terminology/transfusion-service, seeded published v1 by
ensure_lab_transfusion_defaults): standard / extended validity
windows, the extended-eligibility lookback, reservation expiry,
post-thaw expiry per product, special-requirement and unit-
attribute vocabularies, antibody-ID methods, preparation kinds
and their step sequences, disposition + wastage reason codes, and
the severity / imputability grading vocabularies. NOT governed:
the reaction-workup step sequence and the gate matrices (clinical
standards, in code).

Storage: NEW tables only (models.py LAB-D13 block). No existing
table gains a column, so no ensure-columns hook is owed.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir, lab_benches
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .lab_depth import DeviationReport, _retention_block
from .models import (BloodUnit, ConfigStatus, FhirResource,
                     LabAntibodyWorkup, LabHistoricalAntibody,
                     LabReactionWorkup, LabTransfusionRequirement,
                     LabTsSpecimen, LabUnitAttribute, LabUnitDisposition,
                     LabUnitPreparation, LabUnitReservation, StudioConfig,
                     User)
from .security import get_db, require_capability

router = APIRouter(prefix="/lab/transfusion", tags=["lab-transfusion"])

TRANSFUSION_KEY = "transfusion-service"

# The reaction-workup sequence — a FIXED clinical standard (research
# report §1.5), encoded in code like the Wave A compatibility
# matrices: no site may publish its way to a different sequence.
REACTION_STEPS = [
    {"code": "clerical-check",
     "display": "Clerical check (labels, records, unit/patient identity)"},
    {"code": "visual-haemolysis-inspection",
     "display": "Visual haemolysis inspection of the unit / post-transfusion sample"},
    {"code": "dat",
     "display": "Direct antiglobulin test (DAT)"},
    {"code": "repeat-abo-screen-crossmatch",
     "display": "Repeat ABO / antibody screen / crossmatch"},
    {"code": "cultures-coag",
     "display": "Cultures / coagulation studies where indicated "
                "(record 'not-indicated' when not indicated)"},
]


# ---------------------------------------------------------------------------
# Governed configuration
# ---------------------------------------------------------------------------

def _opt(code, display, **extra):
    return {"code": code, "display": display, "active": True, **extra}


def default_transfusion_payload() -> dict:
    """Transfusion-service governed defaults — every figure is a
    GOVERNED DEFAULT (director to set for the site), seeded from the
    researched operating model: 72 h standard T&S validity, 28-day
    extended presurgical validity with a 3-month transfusion /
    pregnancy lookback (UCSF pattern, research §1.5)."""
    return {
        "name": "OpenCare transfusion service (LAB-D13) — GOVERNED "
                "DEFAULTS, director to set",
        "concepts": [{"code": "transfusion-service",
                      "display": "Transfusion service configuration",
                      "active": True}],
        "standardValidityHours": 72,
        "extendedValidityDays": 28,
        "extendedLookbackDays": 90,
        "reservationExpiryHours": 72,
        "postThawExpiryHours": {"plasma": 24, "cryoprecipitate": 6,
                                "platelets": 24, "rbc": 24},
        "specialRequirements": [
            _opt("irradiated", "Irradiated"),
            _opt("cmv-negative", "CMV-negative"),
            _opt("washed", "Washed"),
            _opt("antigen-negative", "Antigen-negative (specificity required)"),
        ],
        "unitAttributes": [
            _opt("irradiated", "Irradiated"),
            _opt("cmv-negative", "CMV-negative"),
            _opt("washed", "Washed"),
            _opt("antigen-negative", "Antigen-negative (specificity required)"),
        ],
        "antibodyIdMethods": [
            _opt("antibody-panel", "Antibody identification panel"),
            _opt("enzyme-panel", "Enzyme-treated panel"),
            _opt("elution", "Elution"),
            _opt("adsorption", "Adsorption"),
            _opt("molecular-genotyping", "Molecular red-cell genotyping"),
        ],
        "prepKinds": [
            _opt("thaw-plasma", "Thaw plasma", products=["plasma"],
                 steps=["thaw-started", "thaw-completed",
                        "visual-inspection", "label-verified"]),
            _opt("thaw-cryoprecipitate", "Thaw cryoprecipitate",
                 products=["cryoprecipitate"],
                 steps=["thaw-started", "thaw-completed",
                        "visual-inspection", "label-verified"]),
            _opt("platelet-prep", "Platelet preparation",
                 products=["platelets"],
                 steps=["received-at-bench", "visual-inspection",
                        "label-verified"]),
            _opt("wash", "Wash red cells", products=["rbc"],
                 steps=["wash-started", "wash-completed",
                        "visual-inspection", "label-verified"]),
        ],
        "prepStepDisplays": {
            "thaw-started": "Thaw started", "thaw-completed": "Thaw completed",
            "wash-started": "Wash started", "wash-completed": "Wash completed",
            "received-at-bench": "Received at bench",
            "visual-inspection": "Visual inspection",
            "label-verified": "Label verified against the order",
        },
        "dispositions": [
            _opt("transfused", "Transfused"),
            _opt("wasted", "Wasted"),
            _opt("discarded", "Discarded"),
            _opt("returned", "Returned to inventory"),
            _opt("expired", "Expired"),
        ],
        "wastageReasonCodes": [
            _opt("expired-before-use", "Expired before use"),
            _opt("temperature-excursion", "Temperature excursion"),
            _opt("damaged-or-leaking", "Damaged or leaking container"),
            _opt("returned-too-late", "Returned outside the return window"),
            _opt("post-thaw-expiry", "Post-thaw expiry reached"),
            _opt("contamination-suspected", "Suspected contamination"),
            _opt("patient-no-longer-requires", "Patient no longer requires the unit"),
            _opt("other-governed", "Other (note required)"),
        ],
        "severityGrades": [
            _opt("mild", "Mild"), _opt("moderate", "Moderate"),
            _opt("severe", "Severe"),
            _opt("life-threatening", "Life-threatening"),
            _opt("death", "Death"),
        ],
        "imputabilityGrades": [
            _opt("definite", "Definite (certain)"),
            _opt("probable", "Probable (likely)"),
            _opt("possible", "Possible"),
            _opt("unlikely", "Unlikely (doubtful)"),
            _opt("excluded", "Excluded"),
            _opt("not-assessable", "Not assessable"),
        ],
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


def ensure_lab_transfusion_defaults(session: Session) -> None:
    """Seed terminology/transfusion-service as published v1
    (idempotent). Coordinator wires this in app.main startup."""
    _seed_published(session, "terminology", TRANSFUSION_KEY,
                    default_transfusion_payload())
    session.commit()


def _cfg(session: Session) -> dict:
    cfg = published_config(session, "terminology", TRANSFUSION_KEY)
    if cfg is not None and isinstance(cfg.payload, dict):
        merged = default_transfusion_payload()
        merged.update(cfg.payload)
        return merged
    return default_transfusion_payload()


def _codes(options) -> set[str]:
    return {o.get("code") for o in options or [] if isinstance(o, dict)}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value):
    """Stored datetimes may come back naive from SQLite; all
    comparisons in this module are against aware UTC now."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_dt(raw, field: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO timestamp")
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an ISO timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


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


def _patient_ref(session: Session, payload: dict) -> str:
    raw = payload.get("patient_ref") or payload.get("patient_id") \
        or payload.get("patient")
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=422,
                            detail="'patient_ref' is required")
    ref = fhir._normalize_patient_ref(raw.strip())
    patient_id = ref.split("/", 1)[-1]
    try:
        fhir._get_row("Patient", patient_id, session)
    except HTTPException:
        raise HTTPException(status_code=422,
                            detail=f"Patient '{ref}' is not on file")
    return ref


def _require_unit(session: Session, unit_id) -> BloodUnit:
    if not isinstance(unit_id, str) or not unit_id.strip():
        raise HTTPException(status_code=422, detail="'unit_id' is required")
    return lab_benches._unit_row(session, unit_id.strip())


def _active_reservation(session: Session,
                        unit_id: str) -> LabUnitReservation | None:
    """The unit's active reservation, honouring governed expiry:
    an active row past its expires_at is expired, not active."""
    row = session.scalars(
        select(LabUnitReservation).where(
            LabUnitReservation.unit_id == unit_id,
            LabUnitReservation.status == "active")
        .order_by(LabUnitReservation.id.desc())).first()
    if row is None:
        return None
    if row.expires_at is not None and _aware(row.expires_at) <= _now():
        return None
    return row


def _reservation_out(row: LabUnitReservation) -> dict:
    effective = row.status
    if effective == "active" and row.expires_at is not None \
            and _aware(row.expires_at) <= _now():
        effective = "expired"
    return {"reservation_id": row.reservation_id, "unit_id": row.unit_id,
            "patient_ref": row.patient_ref, "order_id": row.order_id,
            "status": effective, "stored_status": row.status,
            "expires_at": row.expires_at.isoformat() if row.expires_at
            else None,
            "release_reason": row.release_reason,
            "released_by": row.released_by,
            "released_at": row.released_at.isoformat() if row.released_at
            else None,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at
            else None}


def _create_reservation(session: Session, user: User, unit: BloodUnit,
                        patient_ref: str, order_id) -> LabUnitReservation:
    cfg = _cfg(session)
    try:
        hours = float(cfg.get("reservationExpiryHours") or 72)
    except (TypeError, ValueError):
        hours = 72.0
    row = LabUnitReservation(
        reservation_id=f"res-{uuid.uuid4().hex[:10]}",
        unit_id=unit.unit_id, patient_ref=patient_ref,
        order_id=str(order_id) if order_id else None, status="active",
        expires_at=_now() + timedelta(hours=hours),
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.reservation.create",
                 entity=f"LabUnitReservation/{row.reservation_id}",
                 payload={"unit_id": unit.unit_id,
                          "patient_ref": patient_ref,
                          "expires_at": row.expires_at.isoformat()})
    return row


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------

@router.get("/config")
def transfusion_config(session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.read"))):
    cfg = _cfg(session)
    return {"config": {"kind": "terminology", "key": TRANSFUSION_KEY},
            "standard_validity_hours": cfg.get("standardValidityHours"),
            "extended_validity_days": cfg.get("extendedValidityDays"),
            "extended_lookback_days": cfg.get("extendedLookbackDays"),
            "reservation_expiry_hours": cfg.get("reservationExpiryHours"),
            "post_thaw_expiry_hours": cfg.get("postThawExpiryHours"),
            "special_requirements": cfg.get("specialRequirements") or [],
            "unit_attributes": cfg.get("unitAttributes") or [],
            "antibody_id_methods": cfg.get("antibodyIdMethods") or [],
            "prep_kinds": cfg.get("prepKinds") or [],
            "prep_step_displays": cfg.get("prepStepDisplays") or {},
            "dispositions": cfg.get("dispositions") or [],
            "wastage_reason_codes": cfg.get("wastageReasonCodes") or [],
            "severity_grades": cfg.get("severityGrades") or [],
            "imputability_grades": cfg.get("imputabilityGrades") or [],
            "reaction_steps": REACTION_STEPS}


# ---------------------------------------------------------------------------
# T&S specimen-validity lifecycle
# ---------------------------------------------------------------------------

def _specimen_out(row: LabTsSpecimen) -> dict:
    now = _now()
    expired = row.expires_at is not None and _aware(row.expires_at) <= now
    return {"specimen_id": row.specimen_id, "patient_ref": row.patient_ref,
            "order_id": row.order_id,
            "screen_observation_id": row.screen_observation_id,
            "collected_at": row.collected_at.isoformat()
            if row.collected_at else None,
            "validity_kind": row.validity_kind,
            "extended_requested": bool(row.extended_requested),
            "extended_granted": bool(row.extended_granted),
            "extended_denial_reason": row.extended_denial_reason,
            "transfusion_in_lookback": bool(row.transfusion_in_lookback),
            "pregnancy_in_lookback": bool(row.pregnancy_in_lookback),
            "expires_at": row.expires_at.isoformat() if row.expires_at
            else None,
            "status": "expired" if expired else "valid",
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at
            else None}


@router.post("/ts-specimens", status_code=201)
async def register_ts_specimen(request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability("lab.work"))):
    """Register a type-and-screen specimen and compute its validity
    window. Extended (presurgical) validity is granted ONLY when the
    governed eligibility holds: no transfusion and no pregnancy in
    the governed lookback window — the caller's attestation is
    checked against transfusion Procedures on file for the patient.
    An ineligible extended request still registers the specimen on
    the STANDARD window, with the denial reason named on the record
    (never a silent downgrade)."""
    payload = await _json_body(request)
    cfg = _cfg(session)
    patient_ref = _patient_ref(session, payload)
    collected = _parse_dt(payload.get("collected_at"), "collected_at")
    if collected > _now() + timedelta(minutes=5):
        raise HTTPException(status_code=422,
                            detail="'collected_at' cannot be in the future")
    kind = payload.get("validity_kind") or "standard"
    if kind not in ("standard", "presurgical-extended"):
        raise HTTPException(
            status_code=422,
            detail="'validity_kind' must be 'standard' or "
                   "'presurgical-extended'")
    extended_requested = kind == "presurgical-extended" \
        or payload.get("extended_requested") is True
    transfusion_attested = payload.get("transfusion_in_lookback") is True
    pregnancy_attested = payload.get("pregnancy_in_lookback") is True
    try:
        lookback_days = int(cfg.get("extendedLookbackDays") or 90)
    except (TypeError, ValueError):
        lookback_days = 90
    # System-derived leg: any transfusion Procedure for this patient
    # inside the lookback window defeats eligibility regardless of
    # attestation (the record on file wins over the checkbox).
    cutoff = collected - timedelta(days=lookback_days)
    system_transfusion = False
    for proc in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Procedure",
            FhirResource.patient_ref == patient_ref)).all():
        body = proc.body or {}
        performed = lab_benches._parse_ts(
            body.get("performedDateTime")
            or (body.get("performedPeriod") or {}).get("start"))
        has_unit = any(isinstance(e, dict)
                       and e.get("url") == "urn:opencare:extension/blood-unit"
                       for e in body.get("extension") or [])
        if has_unit and performed is not None and performed >= cutoff \
                and performed <= collected + timedelta(days=lookback_days):
            system_transfusion = True
            break
    transfusion_in = transfusion_attested or system_transfusion
    denial = None
    granted = False
    if extended_requested:
        if transfusion_in:
            denial = (f"transfusion within the governed {lookback_days}-day "
                      "lookback window (attested or on file) — extended "
                      "presurgical validity is not available")
        elif pregnancy_attested:
            denial = (f"pregnancy within the governed {lookback_days}-day "
                      "lookback window — extended presurgical validity "
                      "is not available")
        else:
            granted = True
    if granted:
        try:
            days = float(cfg.get("extendedValidityDays") or 28)
        except (TypeError, ValueError):
            days = 28.0
        expires = collected + timedelta(days=days)
    else:
        try:
            hours = float(cfg.get("standardValidityHours") or 72)
        except (TypeError, ValueError):
            hours = 72.0
        expires = collected + timedelta(hours=hours)
    # Link the specimen to the screen it produced, when one is
    # already released — the gate matches on exactly this link.
    screen = lab_benches._latest_released_result(session, patient_ref,
                                                 "890-4")
    row = LabTsSpecimen(
        specimen_id=f"ts-{uuid.uuid4().hex[:10]}",
        patient_ref=patient_ref,
        order_id=str(payload["order_id"]) if payload.get("order_id") else None,
        screen_observation_id=(payload.get("screen_observation_id")
                               or (screen or {}).get("observation_id")),
        collected_at=collected,
        validity_kind="presurgical-extended" if extended_requested
        else "standard",
        transfusion_in_lookback=transfusion_in,
        pregnancy_in_lookback=pregnancy_attested,
        extended_requested=extended_requested, extended_granted=granted,
        extended_denial_reason=denial, expires_at=expires,
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.ts_specimen.register",
                 entity=f"LabTsSpecimen/{row.specimen_id}",
                 payload={"patient_ref": patient_ref,
                          "extended_granted": granted,
                          "extended_denial_reason": denial,
                          "expires_at": expires.isoformat()})
    session.commit()
    return _specimen_out(row)


@router.get("/ts-specimens")
def list_ts_specimens(patient_ref: str | None = Query(default=None),
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    stmt = select(LabTsSpecimen).order_by(LabTsSpecimen.id.desc())
    if patient_ref:
        stmt = stmt.where(LabTsSpecimen.patient_ref ==
                          fhir._normalize_patient_ref(patient_ref))
    rows = session.scalars(stmt).all()
    items = [_specimen_out(r) for r in rows]
    return {"items": items, "total": len(items),
            "expired": sum(1 for i in items if i["status"] == "expired"),
            "valid": sum(1 for i in items if i["status"] == "valid")}


# ---------------------------------------------------------------------------
# Antibody identification + historical antibody record
# ---------------------------------------------------------------------------

def _historical_out(row: LabHistoricalAntibody) -> dict:
    return {"id": row.id, "patient_ref": row.patient_ref,
            "specificity": row.specificity,
            "clinically_significant": bool(row.clinically_significant),
            "first_identified_on": row.first_identified_on,
            "source": row.source, "source_workup_id": row.source_workup_id,
            "active": bool(row.active), "note": row.note,
            "recorded_by": row.recorded_by,
            "created_at": row.created_at.isoformat() if row.created_at
            else None}


def _ensure_historical(session: Session, user: User, patient_ref: str,
                       specificity: str, clinically_significant: bool,
                       source: str, workup_id: str | None,
                       first_on: str | None) -> LabHistoricalAntibody:
    existing = session.scalars(select(LabHistoricalAntibody).where(
        LabHistoricalAntibody.patient_ref == patient_ref,
        LabHistoricalAntibody.specificity == specificity,
        LabHistoricalAntibody.active.is_(True))).first()
    if existing is not None:
        return existing
    row = LabHistoricalAntibody(
        patient_ref=patient_ref, specificity=specificity,
        clinically_significant=clinically_significant,
        first_identified_on=first_on, source=source,
        source_workup_id=workup_id, active=True,
        recorded_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.historical_antibody.record",
                 entity=f"LabHistoricalAntibody/{row.id}",
                 payload={"patient_ref": patient_ref,
                          "specificity": specificity,
                          "clinically_significant": clinically_significant,
                          "source": source})
    return row


@router.post("/historical-antibodies", status_code=201)
async def record_historical_antibody(request: Request,
                                     session: Session = Depends(get_db),
                                     user: User = Depends(require_capability("lab.work"))):
    """Record a historical antibody directly (e.g. from a prior
    facility's record). Permanent: deactivation is not offered —
    a historical antibody is never deleted, only superseded by a
    newer record naming why (out of scope for this surface)."""
    payload = await _json_body(request)
    patient_ref = _patient_ref(session, payload)
    specificity = payload.get("specificity")
    if not isinstance(specificity, str) or not specificity.strip():
        raise HTTPException(status_code=422,
                            detail="'specificity' is required "
                                   "(e.g. 'anti-E')")
    duplicate = session.scalars(select(LabHistoricalAntibody).where(
        LabHistoricalAntibody.patient_ref == patient_ref,
        LabHistoricalAntibody.specificity == specificity.strip(),
        LabHistoricalAntibody.active.is_(True))).first()
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"An active historical antibody record for "
                   f"'{specificity.strip()}' already exists for "
                   f"{patient_ref} (id {duplicate.id})")
    row = _ensure_historical(
        session, user, patient_ref, specificity.strip(),
        payload.get("clinically_significant") is not False,
        str(payload.get("source") or "external-history"), None,
        str(payload["first_identified_on"])
        if payload.get("first_identified_on") else None)
    if payload.get("note"):
        row.note = str(payload["note"])
    session.commit()
    return _historical_out(row)


@router.get("/historical-antibodies")
def list_historical_antibodies(patient_ref: str | None = Query(default=None),
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability("lab.read"))):
    stmt = select(LabHistoricalAntibody).order_by(
        LabHistoricalAntibody.id.asc())
    if patient_ref:
        stmt = stmt.where(LabHistoricalAntibody.patient_ref ==
                          fhir._normalize_patient_ref(patient_ref))
    rows = session.scalars(stmt).all()
    return {"items": [_historical_out(r) for r in rows], "total": len(rows)}


def _workup_out(row: LabAntibodyWorkup) -> dict:
    return {"workup_id": row.workup_id, "patient_ref": row.patient_ref,
            "method": row.method, "panel_reactions": row.panel_reactions or [],
            "identified_specificities": row.identified_specificities or [],
            "ruled_out": row.ruled_out or [], "conclusion": row.conclusion,
            "recorded_by": row.recorded_by,
            "created_at": row.created_at.isoformat() if row.created_at
            else None}


@router.post("/antibody-workups", status_code=201)
async def create_antibody_workup(request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability("lab.work"))):
    """Record an antibody-identification panel workup. Every
    identified specificity is written to the patient's PERMANENT
    historical antibody record in the same transaction — that record
    is what the Wave A issue gate consults from then on."""
    payload = await _json_body(request)
    cfg = _cfg(session)
    patient_ref = _patient_ref(session, payload)
    method = payload.get("method")
    if method not in _codes(cfg.get("antibodyIdMethods")):
        raise HTTPException(
            status_code=422,
            detail=f"'method' '{method}' is not one of the governed "
                   f"antibody-identification methods "
                   f"{sorted(_codes(cfg.get('antibodyIdMethods')))}")
    reactions = payload.get("panel_reactions")
    if not isinstance(reactions, list) or not reactions:
        raise HTTPException(
            status_code=422,
            detail="'panel_reactions' must be a non-empty list of "
                   "{cell, reaction} panel results")
    clean_reactions = []
    for i, entry in enumerate(reactions):
        if not isinstance(entry, dict) or not str(
                entry.get("cell") or entry.get("antigen") or "").strip() \
                or not str(entry.get("reaction") or "").strip():
            raise HTTPException(
                status_code=422,
                detail=f"panel_reactions[{i}] must name a panel cell/antigen "
                       "and its reaction")
        clean_reactions.append(
            {"cell": str(entry.get("cell") or entry.get("antigen")).strip(),
             "reaction": str(entry["reaction"]).strip()})
    identified = payload.get("identified_specificities")
    if not isinstance(identified, list) or not identified \
            or not all(isinstance(s, str) and s.strip() for s in identified):
        raise HTTPException(
            status_code=422,
            detail="'identified_specificities' must be a non-empty list "
                   "of antibody specificities (use the historical record "
                   "endpoint for a screen-only history)")
    ruled_out = payload.get("ruled_out") or []
    if not isinstance(ruled_out, list) \
            or not all(isinstance(s, str) for s in ruled_out):
        raise HTTPException(status_code=422,
                            detail="'ruled_out' must be a list of "
                                   "specificities ruled out")
    conclusion = payload.get("conclusion")
    if not isinstance(conclusion, str) or not conclusion.strip():
        raise HTTPException(status_code=422,
                            detail="'conclusion' is required")
    clinically_significant = payload.get("clinically_significant") is not False
    row = LabAntibodyWorkup(
        workup_id=f"abid-{uuid.uuid4().hex[:10]}",
        patient_ref=patient_ref, method=method,
        panel_reactions=clean_reactions,
        identified_specificities=[s.strip() for s in identified],
        ruled_out=[s.strip() for s in ruled_out],
        conclusion=conclusion.strip(), recorded_by=user.username)
    session.add(row)
    session.flush()
    historical = []
    for specificity in row.identified_specificities:
        historical.append(_ensure_historical(
            session, user, patient_ref, specificity,
            clinically_significant, "antibody-identification-workup",
            row.workup_id, _now().date().isoformat()))
    append_audit(session, actor=user.username,
                 action="lab.transfusion.antibody_workup.create",
                 entity=f"LabAntibodyWorkup/{row.workup_id}",
                 payload={"patient_ref": patient_ref, "method": method,
                          "identified": row.identified_specificities})
    session.commit()
    return {"workup": _workup_out(row),
            "historical_antibodies": [_historical_out(h) for h in historical]}


@router.get("/antibody-workups")
def list_antibody_workups(patient_ref: str | None = Query(default=None),
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("lab.read"))):
    stmt = select(LabAntibodyWorkup).order_by(LabAntibodyWorkup.id.desc())
    if patient_ref:
        stmt = stmt.where(LabAntibodyWorkup.patient_ref ==
                          fhir._normalize_patient_ref(patient_ref))
    rows = session.scalars(stmt).all()
    return {"items": [_workup_out(r) for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Special requirements (patient standing) + unit attributes
# ---------------------------------------------------------------------------

def _requirement_out(row: LabTransfusionRequirement) -> dict:
    return {"id": row.id, "patient_ref": row.patient_ref,
            "requirement_code": row.requirement_code,
            "specificity": row.specificity, "active": bool(row.active),
            "note": row.note, "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at
            else None}


@router.post("/special-requirements", status_code=201)
async def create_requirement(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    cfg = _cfg(session)
    patient_ref = _patient_ref(session, payload)
    code = payload.get("requirement_code")
    if code not in _codes(cfg.get("specialRequirements")):
        raise HTTPException(
            status_code=422,
            detail=f"'requirement_code' '{code}' is not one of the "
                   f"governed special requirements "
                   f"{sorted(_codes(cfg.get('specialRequirements')))}")
    specificity = payload.get("specificity")
    if code == "antigen-negative" and not (
            isinstance(specificity, str) and specificity.strip()):
        raise HTTPException(
            status_code=422,
            detail="An antigen-negative requirement requires a "
                   "'specificity' (e.g. 'anti-E')")
    duplicate = session.scalars(select(LabTransfusionRequirement).where(
        LabTransfusionRequirement.patient_ref == patient_ref,
        LabTransfusionRequirement.requirement_code == code,
        LabTransfusionRequirement.specificity ==
        (specificity.strip() if isinstance(specificity, str) else None),
        LabTransfusionRequirement.active.is_(True))).first()
    if duplicate is not None:
        raise HTTPException(status_code=409,
                            detail=f"Requirement '{code}' is already "
                                   f"active for {patient_ref}")
    row = LabTransfusionRequirement(
        patient_ref=patient_ref, requirement_code=code,
        specificity=specificity.strip()
        if isinstance(specificity, str) and specificity.strip() else None,
        active=True, note=str(payload.get("note") or "") or None,
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.requirement.create",
                 entity=f"LabTransfusionRequirement/{row.id}",
                 payload={"patient_ref": patient_ref,
                          "requirement_code": code,
                          "specificity": row.specificity})
    session.commit()
    return _requirement_out(row)


@router.get("/special-requirements")
def list_requirements(patient_ref: str | None = Query(default=None),
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    stmt = select(LabTransfusionRequirement).order_by(
        LabTransfusionRequirement.id.asc())
    if patient_ref:
        stmt = stmt.where(LabTransfusionRequirement.patient_ref ==
                          fhir._normalize_patient_ref(patient_ref))
    rows = session.scalars(stmt).all()
    return {"items": [_requirement_out(r) for r in rows], "total": len(rows)}


@router.post("/special-requirements/{requirement_id}/deactivate")
async def deactivate_requirement(requirement_id: int, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(status_code=422,
                            detail="Deactivation requires a 'reason'")
    row = session.scalars(select(LabTransfusionRequirement).where(
        LabTransfusionRequirement.id == requirement_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail="Special requirement not found")
    if not row.active:
        raise HTTPException(status_code=409,
                            detail="Requirement is already inactive")
    row.active = False
    row.note = ((row.note + " | ") if row.note else "") \
        + f"Deactivated: {reason.strip()}"
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.requirement.deactivate",
                 entity=f"LabTransfusionRequirement/{row.id}",
                 payload={"patient_ref": row.patient_ref,
                          "requirement_code": row.requirement_code,
                          "reason": reason.strip()})
    session.commit()
    return _requirement_out(row)


def _attribute_out(row: LabUnitAttribute) -> dict:
    return {"id": row.id, "unit_id": row.unit_id,
            "attribute_code": row.attribute_code,
            "specificity": row.specificity,
            "attested_by": row.attested_by, "note": row.note,
            "created_at": row.created_at.isoformat() if row.created_at
            else None}


@router.post("/unit-attributes", status_code=201)
async def create_unit_attribute(request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(require_capability("lab.work"))):
    """Attest a special-requirement attribute ON A UNIT (the unit
    was irradiated / is CMV-negative / was washed / is antigen-
    negative for a specificity). The gate reads exactly these rows."""
    payload = await _json_body(request)
    cfg = _cfg(session)
    unit = _require_unit(session, payload.get("unit_id"))
    code = payload.get("attribute_code")
    if code not in _codes(cfg.get("unitAttributes")):
        raise HTTPException(
            status_code=422,
            detail=f"'attribute_code' '{code}' is not one of the "
                   f"governed unit attributes "
                   f"{sorted(_codes(cfg.get('unitAttributes')))}")
    specificity = payload.get("specificity")
    if code == "antigen-negative" and not (
            isinstance(specificity, str) and specificity.strip()):
        raise HTTPException(
            status_code=422,
            detail="An antigen-negative unit attribute requires a "
                   "'specificity' (e.g. 'anti-E')")
    spec_value = specificity.strip() \
        if isinstance(specificity, str) and specificity.strip() else None
    duplicate = session.scalars(select(LabUnitAttribute).where(
        LabUnitAttribute.unit_id == unit.unit_id,
        LabUnitAttribute.attribute_code == code,
        LabUnitAttribute.specificity == spec_value)).first()
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Unit {unit.unit_id} already carries the "
                   f"'{code}' attribute"
                   + (f" for '{spec_value}'" if spec_value else ""))
    row = LabUnitAttribute(unit_id=unit.unit_id, attribute_code=code,
                           specificity=spec_value,
                           attested_by=user.username,
                           note=str(payload.get("note") or "") or None)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.unit_attribute.attest",
                 entity=f"BloodUnit/{unit.unit_id}",
                 payload={"attribute_code": code, "specificity": spec_value})
    session.commit()
    return _attribute_out(row)


@router.get("/unit-attributes")
def list_unit_attributes(unit_id: str | None = Query(default=None),
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.read"))):
    stmt = select(LabUnitAttribute).order_by(LabUnitAttribute.id.asc())
    if unit_id:
        stmt = stmt.where(LabUnitAttribute.unit_id == unit_id)
    rows = session.scalars(stmt).all()
    return {"items": [_attribute_out(r) for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------

@router.post("/reservations", status_code=201)
async def create_reservation(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("lab.work"))):
    """Reserve a crossmatched unit for a patient. Requires a
    RELEASED Compatible crossmatch for this unit + patient (the
    reservation rides the crossmatch — the crossmatch-result path
    also creates one automatically; this endpoint covers a unit
    crossmatched before this pass / by explicit bench action)."""
    payload = await _json_body(request)
    unit = _require_unit(session, payload.get("unit_id"))
    patient_ref = _patient_ref(session, payload)
    if unit.status != "available":
        raise HTTPException(
            status_code=409,
            detail=f"Unit {unit.unit_id} is '{unit.status}', not "
                   "available to reserve")
    active = _active_reservation(session, unit.unit_id)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Unit {unit.unit_id} is already reserved for "
                   f"{active.patient_ref} (reservation "
                   f"{active.reservation_id}, expires "
                   f"{active.expires_at.isoformat() if active.expires_at else '—'}) "
                   "— a reserved unit cannot be crossmatched or "
                   "reserved for another patient")
    xm = lab_benches._crossmatch_result_for_unit(session, patient_ref,
                                                 unit.unit_id)
    if xm is None or xm["value"] != "Compatible":
        raise HTTPException(
            status_code=409,
            detail=f"Reservation refused for unit {unit.unit_id}: no "
                   f"released crossmatch 'Compatible' for this unit "
                   f"and {patient_ref} — a reservation rides a "
                   "compatible crossmatch")
    row = _create_reservation(session, user, unit, patient_ref,
                              payload.get("order_id"))
    session.commit()
    return _reservation_out(row)


@router.get("/reservations")
def list_reservations(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(select(LabUnitReservation).order_by(
        LabUnitReservation.id.desc())).all()
    items = [_reservation_out(r) for r in rows]
    return {"items": items, "total": len(items),
            "active": sum(1 for i in items if i["status"] == "active")}


@router.post("/reservations/{reservation_id}/release")
async def release_reservation(reservation_id: str, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(status_code=422,
                            detail="Manual release requires a 'reason'")
    row = session.scalars(select(LabUnitReservation).where(
        LabUnitReservation.reservation_id == reservation_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Reservation '{reservation_id}' not found")
    if row.status != "active":
        raise HTTPException(status_code=409,
                            detail=f"Reservation {reservation_id} is "
                                   f"already '{row.status}'")
    if row.expires_at is not None and _aware(row.expires_at) <= _now():
        row.status = "expired"
        session.commit()
        raise HTTPException(
            status_code=409,
            detail=f"Reservation {reservation_id} has already expired "
                   f"(governed expiry {row.expires_at.isoformat()})")
    row.status = "released"
    row.release_reason = reason.strip()
    row.released_by = user.username
    row.released_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.reservation.release",
                 entity=f"LabUnitReservation/{row.reservation_id}",
                 payload={"unit_id": row.unit_id,
                          "patient_ref": row.patient_ref,
                          "reason": reason.strip(), "path": "manual"})
    session.commit()
    return _reservation_out(row)


# ---------------------------------------------------------------------------
# Thaw / preparation workflow
# ---------------------------------------------------------------------------

def _prep_kind(cfg: dict, code) -> dict | None:
    for kind in cfg.get("prepKinds") or []:
        if isinstance(kind, dict) and kind.get("code") == code:
            return kind
    return None


def _prep_out(session: Session, row: LabUnitPreparation) -> dict:
    cfg = _cfg(session)
    kind = _prep_kind(cfg, row.prep_kind) or {}
    expected = list(kind.get("steps") or [])
    done = [s.get("step") for s in row.steps or []]
    effective = row.status
    if effective == "ready" and row.expires_at is not None \
            and _aware(row.expires_at) <= _now():
        effective = "expired"
    displays = cfg.get("prepStepDisplays") or {}
    return {"prep_id": row.prep_id, "unit_id": row.unit_id,
            "prep_kind": row.prep_kind, "product_type": row.product_type,
            "status": effective, "stored_status": row.status,
            "expected_steps": [{"code": s,
                                "display": displays.get(s, s),
                                "done": s in done} for s in expected],
            "next_step": next((s for s in expected if s not in done), None),
            "steps": row.steps or [],
            "prepared_at": row.prepared_at.isoformat() if row.prepared_at
            else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at
            else None,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at
            else None}


@router.post("/preparations", status_code=201)
async def create_preparation(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    cfg = _cfg(session)
    unit = _require_unit(session, payload.get("unit_id"))
    kind = _prep_kind(cfg, payload.get("prep_kind"))
    if kind is None:
        raise HTTPException(
            status_code=422,
            detail=f"'prep_kind' '{payload.get('prep_kind')}' is not "
                   f"one of the governed preparation kinds "
                   f"{sorted(_codes(cfg.get('prepKinds')))}")
    products = kind.get("products") or []
    if products and unit.product_type not in products:
        raise HTTPException(
            status_code=422,
            detail=f"Preparation '{kind['code']}' applies to "
                   f"{', '.join(products)} units, not "
                   f"'{unit.product_type}' unit {unit.unit_id}")
    open_prep = session.scalars(select(LabUnitPreparation).where(
        LabUnitPreparation.unit_id == unit.unit_id,
        LabUnitPreparation.status.in_(("in-progress", "ready")))).first()
    if open_prep is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Unit {unit.unit_id} already has an open "
                   f"preparation ({open_prep.prep_id}, "
                   f"{open_prep.prep_kind})")
    row = LabUnitPreparation(
        prep_id=f"prep-{uuid.uuid4().hex[:10]}", unit_id=unit.unit_id,
        prep_kind=kind["code"], product_type=unit.product_type,
        steps=[], status="in-progress", created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.preparation.create",
                 entity=f"LabUnitPreparation/{row.prep_id}",
                 payload={"unit_id": unit.unit_id,
                          "prep_kind": kind["code"]})
    session.commit()
    return _prep_out(session, row)


@router.post("/preparations/{prep_id}/steps", status_code=201)
async def record_prep_step(prep_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    """Record the NEXT preparation step (sequence enforced). When
    the final step lands, the preparation is ready and its post-thaw
    expiry is stamped from the governed per-product hours, counted
    from that completion time."""
    payload = await _json_body(request)
    cfg = _cfg(session)
    row = session.scalars(select(LabUnitPreparation).where(
        LabUnitPreparation.prep_id == prep_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Preparation '{prep_id}' not found")
    if row.status != "in-progress":
        raise HTTPException(
            status_code=409,
            detail=f"Preparation {prep_id} is '{row.status}'; steps "
                   "can only be recorded on an in-progress preparation")
    kind = _prep_kind(cfg, row.prep_kind) or {}
    expected = list(kind.get("steps") or [])
    done = [s.get("step") for s in row.steps or []]
    next_step = next((s for s in expected if s not in done), None)
    step = payload.get("step")
    if step != next_step:
        raise HTTPException(
            status_code=409,
            detail=f"Preparation step refused: the next step for "
                   f"{prep_id} is '{next_step}', not '{step}' — "
                   "preparation steps are recorded in sequence")
    at = _parse_dt(payload["at"], "at") if payload.get("at") else _now()
    if row.steps:
        previous = _parse_dt(row.steps[-1]["at"], "at")
        if at < previous:
            raise HTTPException(
                status_code=422,
                detail="Step time 'at' cannot be earlier than the "
                       "previous step's time")
    steps = list(row.steps or [])
    steps.append({"step": step, "at": at.isoformat(),
                  "recorded_by": user.username,
                  "note": str(payload.get("note") or "") or None})
    row.steps = steps
    if len(steps) >= len(expected):
        row.status = "ready"
        row.prepared_at = at
        hours_map = cfg.get("postThawExpiryHours") or {}
        try:
            hours = float(hours_map.get(row.product_type) or 24)
        except (TypeError, ValueError):
            hours = 24.0
        row.expires_at = at + timedelta(hours=hours)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.preparation.step",
                 entity=f"LabUnitPreparation/{row.prep_id}",
                 payload={"unit_id": row.unit_id, "step": step,
                          "status": row.status,
                          "expires_at": row.expires_at.isoformat()
                          if row.expires_at else None})
    session.commit()
    return _prep_out(session, row)


@router.get("/preparations")
def list_preparations(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(select(LabUnitPreparation).order_by(
        LabUnitPreparation.id.desc())).all()
    items = [_prep_out(session, r) for r in rows]
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Dispositions + wastage accounting
# ---------------------------------------------------------------------------

def _disposition_out(row: LabUnitDisposition) -> dict:
    return {"disposition_id": row.disposition_id, "unit_id": row.unit_id,
            "disposition": row.disposition, "reason_code": row.reason_code,
            "note": row.note, "recorded_by": row.recorded_by,
            "created_at": row.created_at.isoformat() if row.created_at
            else None}


@router.post("/dispositions", status_code=201)
async def record_disposition(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("lab.work"))):
    """Record a unit's terminal disposition. Wasted / discarded /
    expired require a governed wastage reason code. Safety bounds:
    'transfused' can only be RECORDED for a unit the BCMA record
    already moved to transfused (this accounting never bypasses
    bedside verification); 'returned' replays the bench return
    semantics (issued -> available, assignment cleared)."""
    payload = await _json_body(request)
    cfg = _cfg(session)
    unit = _require_unit(session, payload.get("unit_id"))
    disposition = payload.get("disposition")
    if disposition not in _codes(cfg.get("dispositions")):
        raise HTTPException(
            status_code=422,
            detail=f"'disposition' '{disposition}' is not one of the "
                   f"governed dispositions "
                   f"{sorted(_codes(cfg.get('dispositions')))}")
    reason = payload.get("reason_code")
    if disposition in ("wasted", "discarded", "expired"):
        if reason not in _codes(cfg.get("wastageReasonCodes")):
            raise HTTPException(
                status_code=422,
                detail=f"A '{disposition}' disposition requires a "
                       f"governed 'reason_code' "
                       f"{sorted(_codes(cfg.get('wastageReasonCodes')))}")
        if reason == "other-governed" and not str(
                payload.get("note") or "").strip():
            raise HTTPException(
                status_code=422,
                detail="Reason 'other-governed' requires a 'note'")
    elif reason is not None and reason not in _codes(
            cfg.get("wastageReasonCodes")):
        raise HTTPException(
            status_code=422,
            detail=f"'reason_code' '{reason}' is not a governed "
                   "wastage reason code")
    if unit.status in ("wasted", "discarded") or (
            unit.status == "expired" and disposition != "expired"):
        raise HTTPException(
            status_code=409,
            detail=f"Unit {unit.unit_id} is already terminal "
                   f"('{unit.status}')")
    if disposition == "transfused" and unit.status != "transfused":
        raise HTTPException(
            status_code=409,
            detail=f"Disposition refused: unit {unit.unit_id} is "
                   f"'{unit.status}', not transfused — 'transfused' "
                   "is recorded only after the BCMA transfusion "
                   "record moves the unit (bedside verification is "
                   "never bypassed by accounting)")
    if disposition == "returned" and unit.status != "issued":
        raise HTTPException(
            status_code=409,
            detail=f"Disposition refused: unit {unit.unit_id} is "
                   f"'{unit.status}', not issued — only an issued "
                   "unit can be returned")
    if disposition in ("wasted", "discarded", "expired") \
            and unit.status == "transfused":
        raise HTTPException(
            status_code=409,
            detail=f"Disposition refused: unit {unit.unit_id} has "
                   "been transfused and cannot be re-dispositioned")
    if disposition == "returned":
        unit.status = "available"
        unit.patient_ref = None
        unit.order_id = None
        unit.issued_at = None
    elif disposition in ("wasted", "discarded", "expired"):
        unit.status = disposition
    row = LabUnitDisposition(
        disposition_id=f"disp-{uuid.uuid4().hex[:10]}",
        unit_id=unit.unit_id, disposition=disposition,
        reason_code=reason, note=str(payload.get("note") or "") or None,
        recorded_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.disposition.record",
                 entity=f"BloodUnit/{unit.unit_id}",
                 payload={"disposition": disposition,
                          "reason_code": reason})
    session.commit()
    return {"disposition": _disposition_out(row),
            "unit_status": unit.status}


@router.get("/dispositions")
def list_dispositions(unit_id: str | None = Query(default=None),
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    stmt = select(LabUnitDisposition).order_by(LabUnitDisposition.id.desc())
    if unit_id:
        stmt = stmt.where(LabUnitDisposition.unit_id == unit_id)
    rows = session.scalars(stmt).all()
    return {"items": [_disposition_out(r) for r in rows], "total": len(rows)}


@router.get("/wastage-report")
def wastage_report(date_from: str | None = Query(default=None),
                   date_to: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    """Wastage accounting: wasted + discarded + expired
    dispositions in the period, grouped by reason code and by
    disposition, with the underlying rows listed."""
    start = _parse_dt(f"{date_from}T00:00:00+00:00", "date_from") \
        if date_from else None
    end = _parse_dt(f"{date_to}T23:59:59+00:00", "date_to") \
        if date_to else None
    rows = session.scalars(select(LabUnitDisposition).where(
        LabUnitDisposition.disposition.in_(
            ("wasted", "discarded", "expired")))
        .order_by(LabUnitDisposition.id.asc())).all()
    items = []
    for row in rows:
        created = row.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if start and (created is None or created < start):
            continue
        if end and (created is None or created > end):
            continue
        items.append(_disposition_out(row))
    by_reason: dict[str, int] = {}
    by_disposition: dict[str, int] = {}
    for item in items:
        by_reason[item["reason_code"] or "unspecified"] = \
            by_reason.get(item["reason_code"] or "unspecified", 0) + 1
        by_disposition[item["disposition"]] = \
            by_disposition.get(item["disposition"], 0) + 1
    return {"items": items, "total": len(items), "by_reason": by_reason,
            "by_disposition": by_disposition,
            "period": {"from": date_from, "to": date_to}}


# ---------------------------------------------------------------------------
# Reaction workup (on Wave A containment) + haemovigilance
# ---------------------------------------------------------------------------

def _reaction_out(row: LabReactionWorkup) -> dict:
    done = {s.get("step"): s for s in row.steps or []}
    expected = [{"code": s["code"], "display": s["display"],
                 "done": s["code"] in done,
                 "result": (done.get(s["code"]) or {}).get("result"),
                 "recorded_by": (done.get(s["code"]) or {}).get("recorded_by"),
                 "recorded_at": (done.get(s["code"]) or {}).get("at")}
                for s in REACTION_STEPS]
    return {"workup_id": row.workup_id, "unit_id": row.unit_id,
            "patient_ref": row.patient_ref, "status": row.status,
            "steps": expected,
            "next_step": next((s["code"] for s in expected
                               if not s["done"]), None),
            "severity": row.severity, "imputability": row.imputability,
            "haemovigilance_report_id": row.haemovigilance_report_id,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at
            else None}


def _reaction_row(session: Session, workup_id: str) -> LabReactionWorkup:
    row = session.scalars(select(LabReactionWorkup).where(
        LabReactionWorkup.workup_id == workup_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Reaction workup '{workup_id}' not found")
    return row


@router.post("/reaction-workups", status_code=201)
async def create_reaction_workup(request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability("lab.work"))):
    """Open the reaction workup for a unit. Containment stays
    FIRST: the unit must already be quarantined by the Wave A
    reaction endpoint (which also placed the patient hold) — this
    endpoint refuses to start a workup on an uncontained unit."""
    payload = await _json_body(request)
    unit = _require_unit(session, payload.get("unit_id"))
    if unit.status != "quarantined":
        raise HTTPException(
            status_code=409,
            detail=f"Reaction workup refused for unit {unit.unit_id}: "
                   f"the unit is '{unit.status}', not quarantined — "
                   "containment comes first: report the reaction "
                   "(POST /lab/blood-bank/units/{unit_id}/reaction) so "
                   "the unit is quarantined and the patient held, "
                   "then open the workup")
    if not unit.patient_ref:
        raise HTTPException(
            status_code=409,
            detail=f"Unit {unit.unit_id} carries no patient "
                   "assignment; a workup needs the implicated patient")
    existing = session.scalars(select(LabReactionWorkup).where(
        LabReactionWorkup.unit_id == unit.unit_id,
        LabReactionWorkup.status == "open")).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"An open reaction workup already exists for unit "
                   f"{unit.unit_id} ({existing.workup_id})")
    row = LabReactionWorkup(
        workup_id=f"rxn-{uuid.uuid4().hex[:10]}", unit_id=unit.unit_id,
        patient_ref=unit.patient_ref, status="open", steps=[],
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.reaction_workup.open",
                 entity=f"LabReactionWorkup/{row.workup_id}",
                 payload={"unit_id": unit.unit_id,
                          "patient_ref": unit.patient_ref})
    session.commit()
    return _reaction_out(row)


@router.post("/reaction-workups/{workup_id}/steps", status_code=201)
async def record_reaction_step(workup_id: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability("lab.work"))):
    """Record the NEXT workup step in the fixed sequence. An
    out-of-order step is refused by name with zero partial write."""
    payload = await _json_body(request)
    row = _reaction_row(session, workup_id)
    if row.status != "open":
        raise HTTPException(
            status_code=409,
            detail=f"Reaction workup {workup_id} is '{row.status}'")
    done = [s.get("step") for s in row.steps or []]
    next_step = next((s["code"] for s in REACTION_STEPS
                      if s["code"] not in done), None)
    step = payload.get("step")
    if step != next_step:
        raise HTTPException(
            status_code=409,
            detail=f"Reaction workup step refused: the workup "
                   f"sequence is fixed and the next step is "
                   f"'{next_step}', not '{step}' — completed steps: "
                   f"{', '.join(done) or 'none'}")
    result = payload.get("result")
    if not isinstance(result, str) or not result.strip():
        raise HTTPException(status_code=422,
                            detail="Each workup step requires a "
                                   "'result'")
    steps = list(row.steps or [])
    steps.append({"step": step, "result": result.strip(),
                  "note": str(payload.get("note") or "") or None,
                  "recorded_by": user.username, "at": _now().isoformat()})
    row.steps = steps
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.reaction_workup.step",
                 entity=f"LabReactionWorkup/{row.workup_id}",
                 payload={"unit_id": row.unit_id, "step": step,
                          "result": result.strip()})
    session.commit()
    return _reaction_out(row)


@router.post("/reaction-workups/{workup_id}/grading")
async def grade_reaction_workup(workup_id: str, request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    cfg = _cfg(session)
    row = _reaction_row(session, workup_id)
    done = {s.get("step") for s in row.steps or []}
    missing = [s["code"] for s in REACTION_STEPS if s["code"] not in done]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"Grading refused: workup steps are incomplete "
                   f"(missing: {', '.join(missing)}) — grading "
                   "follows the completed sequence")
    severity = payload.get("severity")
    imputability = payload.get("imputability")
    if severity not in _codes(cfg.get("severityGrades")):
        raise HTTPException(
            status_code=422,
            detail=f"'severity' '{severity}' is not one of the "
                   f"governed severity grades "
                   f"{sorted(_codes(cfg.get('severityGrades')))}")
    if imputability not in _codes(cfg.get("imputabilityGrades")):
        raise HTTPException(
            status_code=422,
            detail=f"'imputability' '{imputability}' is not one of "
                   f"the governed imputability grades "
                   f"{sorted(_codes(cfg.get('imputabilityGrades')))}")
    row.severity = severity
    row.imputability = imputability
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.reaction_workup.grade",
                 entity=f"LabReactionWorkup/{row.workup_id}",
                 payload={"severity": severity,
                          "imputability": imputability})
    session.commit()
    return _reaction_out(row)


@router.post("/reaction-workups/{workup_id}/haemovigilance-report",
             status_code=201)
async def file_haemovigilance_report(workup_id: str, request: Request,
                                     session: Session = Depends(get_db),
                                     user: User = Depends(require_capability("lab.work"))):
    """Generate the haemovigilance report artefact from a graded
    workup. Filing (honest label): a NEW DeviationReport record
    (report_id hv-...) in the EXISTING biological-product-deviation
    backend (app.lab_depth) — that table, its 21 CFR 606.160
    retention block and its PDF machinery are reused; no lookback
    case is created by this endpoint."""
    payload = await _json_body(request)
    row = _reaction_row(session, workup_id)
    if row.haemovigilance_report_id:
        raise HTTPException(
            status_code=409,
            detail=f"Workup {workup_id} already filed haemovigilance "
                   f"report {row.haemovigilance_report_id}")
    if not row.severity or not row.imputability:
        raise HTTPException(
            status_code=409,
            detail="Haemovigilance report refused: the workup must "
                   "be graded (severity + imputability) before the "
                   "report is generated — grading follows the "
                   "completed step sequence")
    unit = lab_benches._unit_row(session, row.unit_id)
    report_id = f"hv-{uuid.uuid4().hex[:10]}"
    artefact = {
        "type": "haemovigilance-report",
        "basis": "synthetic-data",
        "workup_id": row.workup_id, "unit_id": row.unit_id,
        "patient_ref": row.patient_ref,
        "severity": row.severity, "imputability": row.imputability,
        "steps": row.steps or [],
        "grading": {"severity": row.severity,
                    "imputability": row.imputability},
        "components": [{"unit_id": unit.unit_id,
                        "product_type": unit.product_type,
                        "blood_group": unit.blood_group,
                        "status": unit.status,
                        "patient_ref": row.patient_ref}],
        "recipients": [{"patient_ref": row.patient_ref,
                        "unit_id": row.unit_id}],
        "traceability": _retention_block(),
        "filing_note": "New haemovigilance record filed into the "
                       "existing biological-product-deviation "
                       "backend (lab_deviation_reports); not a "
                       "lookback case.",
        "note": str(payload.get("note") or "") or None,
    }
    report = DeviationReport(
        report_id=report_id, case_id=None, unit_id=row.unit_id,
        description=(f"Haemovigilance report for transfusion reaction "
                     f"workup {row.workup_id} (unit {row.unit_id}, "
                     f"{row.patient_ref}): severity {row.severity}, "
                     f"imputability {row.imputability}."),
        artefact=artefact, pdf_generated=False,
        document_note=("Haemovigilance artefact filed from the "
                       "LAB-D13 reaction workup; PDF rendering rides "
                       "the existing deviation document machinery."),
        created_by=user.username)
    session.add(report)
    row.haemovigilance_report_id = report_id
    row.status = "complete"
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.transfusion.haemovigilance.file",
                 entity=f"DeviationReport/{report_id}",
                 payload={"workup_id": row.workup_id,
                          "unit_id": row.unit_id,
                          "severity": row.severity,
                          "imputability": row.imputability})
    session.commit()
    return {"workup": _reaction_out(row),
            "haemovigilance_report": {
                "report_id": report_id, "type": "haemovigilance-report",
                "filed_into": "lab_deviation_reports (existing "
                              "biological-product-deviation backend, "
                              "app.lab_depth)",
                "artefact": artefact}}


@router.get("/reaction-workups")
def list_reaction_workups(session: Session = Depends(get_db),
                          user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(select(LabReactionWorkup).order_by(
        LabReactionWorkup.id.desc())).all()
    return {"items": [_reaction_out(r) for r in rows], "total": len(rows)}


@router.get("/reaction-workups/{workup_id}")
def get_reaction_workup(workup_id: str, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.read"))):
    return _reaction_out(_reaction_row(session, workup_id))
