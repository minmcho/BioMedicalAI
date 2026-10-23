"""Research & R&D operations — Phase 16, WS-F2 (Scope H + BD-42).

What lives here:

- **Studies** (``ResearchStudy``): protocol id, title, phase, PI, and a
  lifecycle ``draft -> active -> closed``. Protocol approval is a
  governed act: it REQUIRES an electronic signature through
  app.esignature over the study's canonical snapshot (approval without
  a signature is a 422 — BD-46 applied here).
- **Procedures + coverage determinations** (BD-42 / CTMS coverage
  analysis): every procedure on a study carries a coverage
  determination — ``research | clinical | mixed`` with a rationale, the
  determining user, and an optional e-signature reference (a signature
  over the StudyProcedure record, via app.esignature). Determinations
  are append-only history; the CURRENT determination is the latest.
- **Activation gate**: a study may activate only when it is approved
  (approval signature on file) AND every procedure carries a current
  determination. The refusal lists the undetermined procedures.
- **Billing grid** (BD-42): per-study charge lines separating
  research-billed from standard-billed amounts with exact totals. A
  grid RELEASE produces the export artefact: the grid's lines tagged
  ``research`` / ``standard`` for the revenue cycle. The release does
  NOT create draft FHIR claim lines: app.billing generates Claim lines
  only from a patient Encounter (``generate_claim``) and app.revenue
  has no standalone draft-line helper — grid lines are study-level and
  patient-less, so the tagged export payload is the clean interface
  (see the integration note). Every release is audited.
- **Regulatory packs**: GCP (``gcp-ich-e6``), GLP
  (``glp-21cfr58-oecd``) and 21 CFR Part 11 (``cfr-part-11``) seeded as
  DRAFT ``regulatory_pack`` Studio configs via the compliance
  insert-if-absent pattern — labelled drafts; legal attestation is
  out of scope. Requirements-REGISTER entries for these bodies belong
  in app.compliance.default_requirement_seeds() (coordinator-owned
  central seed; proposed entries are listed in the integration note).

Consistency with the Phase 9 research workbench (app.pophealth):
studies reference cohorts by key only — cohort definitions stay
published Studio ``cohort`` configs, snapshots stay aggregate counts,
and nothing here exports patient-level rows, exactly like the
workbench's aggregate-only posture.
"""
import hashlib  # noqa: F401  (kept for parity with sibling modules)
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (DateTime, Float, Integer, String, Text, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import esignature
from .audit import append_audit
from .configstore import (CAPABILITY_DEFAULTS, list_config_versions,
                          published_config, role_allowed)
from .db import Base
from .models import ConfigStatus, StudioConfig, User, utcnow
from .security import get_current_user, get_db

router = APIRouter(prefix="/research", tags=["research"])

STUDY_STATUSES = ("draft", "active", "closed")
DETERMINATIONS = ("research", "clinical", "mixed")
BILLED_TO = ("research", "standard")

GCP_KEY = "gcp-ich-e6"
GLP_KEY = "glp-21cfr58-oecd"

# Proposed capability defaults (coordinator wires these into
# app.configstore.CAPABILITY_DEFAULTS centrally; the local fallback
# applies the SAME tuples until then — see app.esignature).
_CAP_FALLBACKS = {
    "research.read": ("admin", "configurator", "clinician", "viewer"),
    "research.work": ("admin", "configurator", "clinician"),
    "esign.sign": ("admin", "configurator", "clinician"),
}


def _cap_allowed(session: Session, user: User, capability: str) -> bool:
    cfg = published_config(session, "permissions", "role-matrix")
    if cfg is not None:
        overrides = ((cfg.payload or {}).get("capabilities") or {}).get(capability)
        if isinstance(overrides, dict) and user.role.value in overrides:
            return bool(overrides[user.role.value])
    if capability in CAPABILITY_DEFAULTS:
        return role_allowed(session, user.role, capability)
    return user.role.value in _CAP_FALLBACKS.get(capability, ())


def _require(capability: str):
    def dep(session: Session = Depends(get_db),
            user: User = Depends(get_current_user)) -> User:
        if not _cap_allowed(session, user, capability):
            raise HTTPException(
                status_code=403,
                detail=f"Role {user.role.value} lacks capability '{capability}'")
        return user
    return dep


def _money(value) -> float:
    return round(float(value), 2)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

class ResearchStudy(Base):
    __tablename__ = "research_studies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    protocol_id: Mapped[str] = mapped_column(String(120), unique=True,
                                             nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    phase: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="draft", index=True)
    pi_username: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    approval_signature_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


class StudyProcedure(Base):
    __tablename__ = "research_study_procedures"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    study_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    procedure_code: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(400), nullable=False,
                                             default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class CoverageDetermination(Base):
    __tablename__ = "research_coverage_determinations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    procedure_id: Mapped[int] = mapped_column(Integer, nullable=False,
                                              index=True)
    study_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    determination: Mapped[str] = mapped_column(String(20), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    determined_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                              default="")
    signature_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class BillingGridLine(Base):
    __tablename__ = "research_billing_grid_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    study_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    procedure_id: Mapped[int] = mapped_column(Integer, nullable=False,
                                              index=True)
    charge_code: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    billed_to: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Canonical snapshots (registered into app.esignature)
# ---------------------------------------------------------------------------

def _study_snapshot(session: Session, record_id: str) -> dict | None:
    try:
        study = session.get(ResearchStudy, int(record_id))
    except (TypeError, ValueError):
        study = None
    if study is None:
        study = session.scalars(select(ResearchStudy).where(
            ResearchStudy.protocol_id == str(record_id))).first()
    if study is None:
        return None
    return {"id": study.id, "protocolId": study.protocol_id,
            "title": study.title, "phase": study.phase,
            "status": study.status, "piUsername": study.pi_username}


def _procedure_snapshot(session: Session, record_id: str) -> dict | None:
    try:
        proc = session.get(StudyProcedure, int(record_id))
    except (TypeError, ValueError):
        return None
    if proc is None:
        return None
    return {"id": proc.id, "studyId": proc.study_id,
            "procedureCode": proc.procedure_code,
            "description": proc.description}


def _determination_snapshot(session: Session, record_id: str) -> dict | None:
    try:
        det = session.get(CoverageDetermination, int(record_id))
    except (TypeError, ValueError):
        return None
    if det is None:
        return None
    return {"id": det.id, "procedureId": det.procedure_id,
            "studyId": det.study_id, "determination": det.determination,
            "rationale": det.rationale, "determinedBy": det.determined_by}


esignature.register_snapshot_fn("ResearchStudy", _study_snapshot)
esignature.register_snapshot_fn("StudyProcedure", _procedure_snapshot)
esignature.register_snapshot_fn("CoverageDetermination",
                                _determination_snapshot)


# ---------------------------------------------------------------------------
# Serialisers + lookups
# ---------------------------------------------------------------------------

def _study_out(session: Session, study: ResearchStudy) -> dict:
    return {"id": study.id, "protocol_id": study.protocol_id,
            "title": study.title, "phase": study.phase,
            "status": study.status, "pi_username": study.pi_username,
            "approved": study.approval_signature_id is not None,
            "approval_signature_id": study.approval_signature_id,
            "created_by": study.created_by,
            "created_at": _aware(study.created_at).isoformat()
            if study.created_at else None,
            "snapshot_hash": esignature.compute_snapshot_hash(
                session, "ResearchStudy", str(study.id))}


def _procedure_out(session: Session, proc: StudyProcedure) -> dict:
    det = _current_determination(session, proc.id)
    return {"id": proc.id, "study_id": proc.study_id,
            "procedure_code": proc.procedure_code,
            "description": proc.description,
            "determination": _determination_out(det) if det else None,
            "snapshot_hash": esignature.compute_snapshot_hash(
                session, "StudyProcedure", str(proc.id))}


def _determination_out(det: CoverageDetermination | None) -> dict | None:
    if det is None:
        return None
    return {"id": det.id, "procedure_id": det.procedure_id,
            "study_id": det.study_id, "determination": det.determination,
            "rationale": det.rationale, "determined_by": det.determined_by,
            "signature_id": det.signature_id,
            "created_at": _aware(det.created_at).isoformat()
            if det.created_at else None}


def _grid_line_out(line: BillingGridLine) -> dict:
    return {"id": line.id, "study_id": line.study_id,
            "procedure_id": line.procedure_id,
            "charge_code": line.charge_code,
            "amount": _money(line.amount), "billed_to": line.billed_to}


def _study_row(session: Session, study_id) -> ResearchStudy:
    study = None
    try:
        study = session.get(ResearchStudy, int(study_id))
    except (TypeError, ValueError):
        study = None
    if study is None:
        study = session.scalars(select(ResearchStudy).where(
            ResearchStudy.protocol_id == str(study_id))).first()
    if study is None:
        raise HTTPException(status_code=404,
                            detail=f"Research study '{study_id}' not found")
    return study


def _procedure_row(session: Session, procedure_id) -> StudyProcedure:
    try:
        proc = session.get(StudyProcedure, int(procedure_id))
    except (TypeError, ValueError):
        proc = None
    if proc is None:
        raise HTTPException(
            status_code=404,
            detail=f"Study procedure '{procedure_id}' not found")
    return proc


def _current_determination(session: Session,
                           procedure_id: int) -> CoverageDetermination | None:
    return session.scalars(
        select(CoverageDetermination)
        .where(CoverageDetermination.procedure_id == procedure_id)
        .order_by(CoverageDetermination.id.desc())).first()


def _procedures(session: Session, study_id: int) -> list[StudyProcedure]:
    return list(session.scalars(
        select(StudyProcedure).where(StudyProcedure.study_id == study_id)
        .order_by(StudyProcedure.id.asc())).all())


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


def _field(payload: dict, *names: str):
    for name in names:
        if payload.get(name) is not None:
            return payload[name]
    return None


def _require_str(payload: dict, *names: str) -> str:
    value = _field(payload, *names)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{names[0]}' is required")
    return value.strip()


# ---------------------------------------------------------------------------
# Signature resolution for governed actions (approval / determinations)
# ---------------------------------------------------------------------------

def _resolve_signature(session: Session, payload: dict, *, record_type: str,
                       record_id: str, user: User) -> int:
    """Resolve the e-signature for a governed action from EITHER an
    inline ``esign`` block ({meaning, snapshotHash} — signed here, in
    this transaction, through app.esignature) OR a ``signatureId``
    naming an existing SignatureRecord over the same record by the
    same signer. No signature at all -> 422."""
    esign_block = payload.get("esign") if isinstance(payload.get("esign"), dict) \
        else None
    signature_id = _field(payload, "signatureId", "signature_id")
    if esign_block is not None:
        row = esignature.sign_record(
            session, record_type=record_type, record_id=str(record_id),
            signer=user.username,
            meaning=esign_block.get("meaning"),
            snapshot_hash=esign_block.get("snapshotHash")
            or esign_block.get("snapshot_hash"))
        return row.id
    if signature_id is None:
        raise HTTPException(
            status_code=422,
            detail="This action requires an electronic signature: supply "
                   "'signatureId' (a signature over this record via "
                   "POST /esign/sign) or an 'esign' block {meaning, "
                   "snapshotHash}")
    try:
        sig = session.get(esignature.SignatureRecord, int(signature_id))
    except (TypeError, ValueError):
        sig = None
    if sig is None:
        raise HTTPException(status_code=422,
                            detail=f"Signature {signature_id} not found")
    if sig.record_type != record_type or sig.record_id != str(record_id):
        raise HTTPException(
            status_code=422,
            detail=f"Signature {signature_id} covers "
                   f"{sig.record_type}/{sig.record_id}, not "
                   f"{record_type}/{record_id}")
    if sig.signer_username != user.username:
        raise HTTPException(
            status_code=422,
            detail=f"Signature {signature_id} belongs to signer "
                   f"'{sig.signer_username}', not the caller")
    return sig.id


# ---------------------------------------------------------------------------
# Study CRUD
# ---------------------------------------------------------------------------

@router.post("/studies", status_code=201)
async def create_study(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(_require("research.work"))):
    payload = await _json_body(request)
    protocol_id = _require_str(payload, "protocolId", "protocol_id")
    title = _require_str(payload, "title")
    phase = str(_field(payload, "phase") or "")
    pi = str(_field(payload, "piUsername", "pi_username")
             or user.username)
    if session.scalars(select(ResearchStudy).where(
            ResearchStudy.protocol_id == protocol_id)).first() is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A study with protocol_id '{protocol_id}' already exists")
    study = ResearchStudy(protocol_id=protocol_id, title=title, phase=phase,
                          status="draft", pi_username=pi,
                          created_by=user.username)
    session.add(study)
    session.flush()
    append_audit(session, actor=user.username, action="research.study.create",
                 entity=f"ResearchStudy/{study.id}",
                 payload={"protocol_id": protocol_id, "title": title,
                          "phase": phase, "pi": pi})
    session.commit()
    return _study_out(session, study)


@router.get("/studies")
def list_studies(status: str | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(_require("research.read"))):
    stmt = select(ResearchStudy).order_by(ResearchStudy.id.asc())
    if status:
        stmt = stmt.where(ResearchStudy.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_study_out(session, s) for s in rows],
            "total": len(rows)}


@router.get("/studies/{study_id}")
def get_study(study_id: str, session: Session = Depends(get_db),
              user: User = Depends(_require("research.read"))):
    study = _study_row(session, study_id)
    out = _study_out(session, study)
    out["procedures"] = [_procedure_out(session, p)
                         for p in _procedures(session, study.id)]
    return out


@router.put("/studies/{study_id}")
async def update_study(study_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(_require("research.work"))):
    """Edit the reviewable study fields (title / phase / PI). Any edit
    changes the canonical snapshot, so a snapshot hash taken before
    the edit can no longer be signed against (esignature 409)."""
    study = _study_row(session, study_id)
    if study.status == "closed":
        raise HTTPException(status_code=409,
                            detail="A closed study cannot be edited")
    payload = await _json_body(request)
    if _field(payload, "title") is not None:
        study.title = _require_str(payload, "title")
    if _field(payload, "phase") is not None:
        study.phase = str(payload["phase"])
    if _field(payload, "piUsername", "pi_username") is not None:
        study.pi_username = str(_field(payload, "piUsername", "pi_username"))
    session.flush()
    append_audit(session, actor=user.username, action="research.study.update",
                 entity=f"ResearchStudy/{study.id}",
                 payload={"protocol_id": study.protocol_id})
    session.commit()
    return _study_out(session, study)


# ---------------------------------------------------------------------------
# Procedures + coverage determinations (BD-42)
# ---------------------------------------------------------------------------

@router.post("/studies/{study_id}/procedures", status_code=201)
async def add_procedure(study_id: str, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(_require("research.work"))):
    study = _study_row(session, study_id)
    if study.status == "closed":
        raise HTTPException(status_code=409,
                            detail="A closed study takes no new procedures")
    payload = await _json_body(request)
    code = _require_str(payload, "procedureCode", "procedure_code")
    description = str(_field(payload, "description") or "")
    proc = StudyProcedure(study_id=study.id, procedure_code=code,
                          description=description)
    session.add(proc)
    session.flush()
    append_audit(session, actor=user.username,
                 action="research.procedure.create",
                 entity=f"StudyProcedure/{proc.id}",
                 payload={"study_id": study.id, "procedure_code": code})
    session.commit()
    return _procedure_out(session, proc)


@router.get("/studies/{study_id}/procedures")
def list_procedures(study_id: str, session: Session = Depends(get_db),
                    user: User = Depends(_require("research.read"))):
    study = _study_row(session, study_id)
    procs = _procedures(session, study.id)
    return {"study_id": study.id,
            "items": [_procedure_out(session, p) for p in procs],
            "total": len(procs)}


@router.post("/procedures/{procedure_id}/determination", status_code=201)
async def determine_coverage(procedure_id: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(_require("research.work"))):
    """Record the coverage determination for one procedure (BD-42):
    {determination: research|clinical|mixed, rationale, signatureId?
    | esign?}. The e-signature, when supplied, must be a signature
    over the StudyProcedure record by the caller (BD-46 applied at
    coverage determinations); it is stored on the determination."""
    proc = _procedure_row(session, procedure_id)
    payload = await _json_body(request)
    determination = _field(payload, "determination")
    if determination not in DETERMINATIONS:
        raise HTTPException(
            status_code=422,
            detail=f"'determination' must be one of {list(DETERMINATIONS)}")
    rationale = str(_field(payload, "rationale") or "")
    signature_id = None
    if _field(payload, "signatureId", "signature_id") is not None \
            or isinstance(payload.get("esign"), dict):
        signature_id = _resolve_signature(
            session, payload, record_type="StudyProcedure",
            record_id=str(proc.id), user=user)
    det = CoverageDetermination(
        procedure_id=proc.id, study_id=proc.study_id,
        determination=determination, rationale=rationale,
        determined_by=user.username, signature_id=signature_id)
    session.add(det)
    session.flush()
    append_audit(session, actor=user.username,
                 action="research.coverage.determine",
                 entity=f"StudyProcedure/{proc.id}",
                 payload={"determination": determination,
                          "determination_id": det.id,
                          "signature_id": signature_id,
                          "study_id": proc.study_id})
    session.commit()
    return _determination_out(det)


@router.get("/studies/{study_id}/coverage")
def study_coverage(study_id: str, session: Session = Depends(get_db),
                   user: User = Depends(_require("research.read"))):
    """The CTMS coverage view of one study: every procedure with its
    current determination, plus the undetermined list the activation
    gate enforces (BD-42)."""
    study = _study_row(session, study_id)
    items, undetermined = [], []
    for proc in _procedures(session, study.id):
        out = _procedure_out(session, proc)
        items.append(out)
        if out["determination"] is None:
            undetermined.append({"procedure_id": proc.id,
                                 "procedure_code": proc.procedure_code})
    return {"study_id": study.id, "protocol_id": study.protocol_id,
            "items": items, "undetermined": undetermined,
            "total": len(items),
            "coverage_complete": not undetermined}


# ---------------------------------------------------------------------------
# Protocol approval (e-signature required) + lifecycle gates
# ---------------------------------------------------------------------------

@router.post("/studies/{study_id}/approve")
async def approve_protocol(study_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(_require("research.work"))):
    """Approve the protocol. REQUIRES an e-signature over the
    ResearchStudy snapshot (BD-46): no signature -> 422; a stale
    snapshot -> 409 (via app.esignature); a signature over a different
    record or by a different signer -> 422."""
    study = _study_row(session, study_id)
    if study.status != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"Study is '{study.status}'; only a draft protocol "
                   "can be approved")
    payload = await _json_body(request)
    signature_id = _resolve_signature(
        session, payload, record_type="ResearchStudy",
        record_id=str(study.id), user=user)
    study.approval_signature_id = signature_id
    session.flush()
    append_audit(session, actor=user.username,
                 action="research.protocol.approve",
                 entity=f"ResearchStudy/{study.id}",
                 payload={"protocol_id": study.protocol_id,
                          "signature_id": signature_id})
    session.commit()
    return _study_out(session, study)


@router.post("/studies/{study_id}/activate")
def activate_study(study_id: str, session: Session = Depends(get_db),
                   user: User = Depends(_require("research.work"))):
    """Activate a study. Gates (BD-42): the protocol must be approved
    (approval signature on file) and EVERY procedure must carry a
    coverage determination — the refusal lists the undetermined
    procedures so the coverage analysis can be completed."""
    study = _study_row(session, study_id)
    if study.status != "draft":
        raise HTTPException(
            status_code=409,
            detail=f"Study is '{study.status}'; only a draft can activate")
    if study.approval_signature_id is None:
        raise HTTPException(
            status_code=422,
            detail="Study cannot activate: the protocol has not been "
                   "approved (POST /research/studies/{id}/approve with "
                   "an e-signature first)")
    undetermined = [{"procedure_id": p.id, "procedure_code": p.procedure_code}
                    for p in _procedures(session, study.id)
                    if _current_determination(session, p.id) is None]
    if undetermined:
        raise HTTPException(
            status_code=422,
            detail={"error": ("Study cannot activate: every procedure on "
                              "an active study must carry a coverage "
                              "determination (BD-42)"),
                    "undetermined_procedures": undetermined})
    study.status = "active"
    session.flush()
    append_audit(session, actor=user.username,
                 action="research.study.activate",
                 entity=f"ResearchStudy/{study.id}",
                 payload={"protocol_id": study.protocol_id})
    session.commit()
    return _study_out(session, study)


@router.post("/studies/{study_id}/close")
def close_study(study_id: str, session: Session = Depends(get_db),
                user: User = Depends(_require("research.work"))):
    study = _study_row(session, study_id)
    if study.status == "closed":
        raise HTTPException(status_code=409,
                            detail="Study is already closed")
    study.status = "closed"
    session.flush()
    append_audit(session, actor=user.username, action="research.study.close",
                 entity=f"ResearchStudy/{study.id}",
                 payload={"protocol_id": study.protocol_id})
    session.commit()
    return _study_out(session, study)


# ---------------------------------------------------------------------------
# Billing grid (BD-42): research-billed vs standard-billed, exact totals
# ---------------------------------------------------------------------------

@router.post("/studies/{study_id}/grid-lines", status_code=201)
async def add_grid_line(study_id: str, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(_require("research.work"))):
    study = _study_row(session, study_id)
    payload = await _json_body(request)
    procedure_id = _field(payload, "procedureId", "procedure_id")
    proc = _procedure_row(session, procedure_id) \
        if procedure_id is not None else None
    if proc is None or proc.study_id != study.id:
        raise HTTPException(
            status_code=422,
            detail="'procedureId' must name a procedure of this study")
    charge_code = _require_str(payload, "chargeCode", "charge_code")
    amount = _field(payload, "amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) \
            or float(amount) < 0:
        raise HTTPException(status_code=422,
                            detail="'amount' must be a non-negative number")
    billed_to = _field(payload, "billedTo", "billed_to")
    if billed_to not in BILLED_TO:
        raise HTTPException(
            status_code=422,
            detail=f"'billedTo' must be one of {list(BILLED_TO)}")
    line = BillingGridLine(study_id=study.id, procedure_id=proc.id,
                           charge_code=charge_code, amount=_money(amount),
                           billed_to=billed_to, created_by=user.username)
    session.add(line)
    session.flush()
    append_audit(session, actor=user.username,
                 action="research.grid.line.create",
                 entity=f"ResearchStudy/{study.id}",
                 payload={"line_id": line.id, "charge_code": charge_code,
                          "amount": line.amount, "billed_to": billed_to,
                          "procedure_id": proc.id})
    session.commit()
    return _grid_line_out(line)


def _grid(session: Session, study: ResearchStudy) -> dict:
    lines = list(session.scalars(
        select(BillingGridLine).where(BillingGridLine.study_id == study.id)
        .order_by(BillingGridLine.id.asc())).all())
    proc_codes = {p.id: p.procedure_code
                  for p in _procedures(session, study.id)}
    out_lines = []
    research_total = standard_total = 0.0
    for line in lines:
        entry = _grid_line_out(line)
        entry["procedure_code"] = proc_codes.get(line.procedure_id)
        out_lines.append(entry)
        if line.billed_to == "research":
            research_total += line.amount
        else:
            standard_total += line.amount
    research_total, standard_total = _money(research_total), _money(standard_total)
    return {"study_id": study.id, "protocol_id": study.protocol_id,
            "lines": out_lines,
            "research_total": research_total,
            "standard_total": standard_total,
            "grand_total": _money(research_total + standard_total),
            "research_count": sum(1 for l in lines
                                  if l.billed_to == "research"),
            "standard_count": sum(1 for l in lines
                                  if l.billed_to == "standard"),
            "total": len(lines)}


@router.get("/studies/{study_id}/billing-grid")
def get_billing_grid(study_id: str, session: Session = Depends(get_db),
                     user: User = Depends(_require("research.read"))):
    study = _study_row(session, study_id)
    return _grid(session, study)


@router.post("/studies/{study_id}/billing-grid/release")
def release_billing_grid(study_id: str, session: Session = Depends(get_db),
                         user: User = Depends(_require("research.work"))):
    """Release the grid to the revenue cycle as an EXPORT PAYLOAD of
    tagged charge lines (tag = research | standard, mirroring
    billed_to). No draft FHIR claim lines are created: billing claims
    are generated from patient encounters (app.billing.generate_claim)
    and grid lines are study-level / patient-less, so the tagged
    payload is the artefact the revenue cycle consumes (see module
    docstring + integration note). The release is audited."""
    study = _study_row(session, study_id)
    grid = _grid(session, study)
    if not grid["lines"]:
        raise HTTPException(status_code=422,
                            detail="Billing grid is empty; nothing to release")
    export_lines = [{"charge_code": l["charge_code"],
                     "procedure_code": l["procedure_code"],
                     "procedure_id": l["procedure_id"],
                     "amount": l["amount"], "billed_to": l["billed_to"],
                     "tag": l["billed_to"],
                     "source": "opencare-research-grid",
                     "protocol_id": study.protocol_id}
                    for l in grid["lines"]]
    payload = {"study_id": study.id, "protocol_id": study.protocol_id,
               "released_by": user.username,
               "released_at": datetime.now(timezone.utc).isoformat(),
               "charge_lines": export_lines,
               "research_total": grid["research_total"],
               "standard_total": grid["standard_total"],
               "grand_total": grid["grand_total"],
               "line_count": len(export_lines)}
    append_audit(session, actor=user.username,
                 action="research.grid.release",
                 entity=f"ResearchStudy/{study.id}",
                 payload={"protocol_id": study.protocol_id,
                          "line_count": len(export_lines),
                          "research_total": grid["research_total"],
                          "standard_total": grid["standard_total"],
                          "grand_total": grid["grand_total"]})
    session.commit()
    return payload


# ---------------------------------------------------------------------------
# Regulatory pack seeds (DRAFTS — compliance insert-if-absent pattern)
# ---------------------------------------------------------------------------

def default_gcp_pack_payload() -> dict:
    return {
        "body": "ICH-GCP",
        "jurisdiction": "International (ICH)",
        "parameters": {
            "monitoringVisitCadenceDays": 90,
            "monitoringCadence": "risk-based: on-site every 90 days "
                                 "plus central monitoring",
            "essentialDocuments": [
                "protocol", "protocol-amendments", "informed-consent-form",
                "investigator-brochure", "irb-iec-approval",
                "delegation-log", "training-records",
                "monitoring-visit-reports", "safety-reports",
                "case-report-forms"],
        },
        "requirements": ["ICH E6(R2) 5.18", "ICH E6(R2) 8.1",
                         "ICH E6(R2) 4.5", "ICH E6(R2) 5.5"],
        "notes": ("ICH E6 Good Clinical Practice baseline — DRAFT, "
                  "legal attestation out of scope. Monitoring cadence "
                  "and the essential-documents list (ICH E6 §8) are "
                  "pack parameters a site reviews and publishes "
                  "through governance. Seeded as a draft "
                  "(Phase 16, WS-F2 / Scope H)."),
    }


def default_glp_pack_payload() -> dict:
    return {
        "body": "FDA-GLP",
        "jurisdiction": "US",
        "parameters": {
            "qaAuditCadenceDays": 180,
            "qaAuditCadence": "semi-annual QA unit audits plus "
                              "study-phase inspections",
            "rawDataRetentionYears": 5,
        },
        "requirements": ["21 CFR 58.15", "21 CFR 58.29",
                         "21 CFR 58.195", "OECD GLP Principles"],
        "notes": ("21 CFR Part 58 / OECD Good Laboratory Practice "
                  "baseline — DRAFT, legal attestation out of scope. "
                  "QA audit cadence and raw-data retention years are "
                  "pack parameters. Seeded as a draft (Phase 16, "
                  "WS-F2 / Scope H)."),
    }


def ensure_research_defaults(session: Session) -> None:
    """Seed the GCP + GLP + Part 11 regulatory packs as DRAFTS
    (insert-if-absent, the app.compliance pattern). Part 11's payload
    is owned by app.esignature; seeding it here too keeps the three
    research packs together — either ensure order is safe."""
    esignature._seed_pack_draft(session, GCP_KEY,
                                default_gcp_pack_payload())
    esignature._seed_pack_draft(session, GLP_KEY,
                                default_glp_pack_payload())
    esignature._seed_pack_draft(session, esignature.PART11_KEY,
                                esignature.default_part11_pack_payload())
    session.commit()
