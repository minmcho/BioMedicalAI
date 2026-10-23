"""Phase 16, WS-G1 (BD-47) — EMPI merge governance.

Phase 14's MPI dedup scan (app.pipelines) produces DedupCandidate
review items and NEVER merges. This module is the governed merge
that Phase 14 deliberately left out:

- **Candidates**: read cleanly from the Phase 14 `DedupCandidate`
  table (the pipeline outputs) AND creatable manually from an
  explicit {sourceId, survivorId} pair (which lands as a
  DedupCandidate review row, so manual and scanned candidates share
  one review surface). Reading or creating a candidate NEVER merges
  anything.
- **MergeRequest workflow**: proposed -> approved -> executed, with
  rejected and reversed as the other terminal/rollback states. The
  approver must be a DIFFERENT steward from the proposer (enforced
  server-side, 403). Execute additionally requires an explicit
  `confirm: true` (missing/false -> 422) and an approved request
  (anything else -> 409). There is NO other code path in this module
  that mutates a Patient, and no silent/auto-merge anywhere.
- **Execute** (one transaction): the MergeCrossReference row —
  carrying BOTH patients' identifier lists and the FULL pre-merge
  snapshot JSON of both patients — is written and flushed FIRST;
  only then is the survivor updated per the request's per-field
  choices (each field taken from `source` or kept from `survivor`)
  and the source set `active: false` with a FHIR `link`
  [{other: {reference: Patient/<survivor>}, type: "replaced-by"}].
  Patient writes mutate the FhirResource rows through app.fhir's
  own store helpers (`_get_row` / `_apply_projections`) inside the
  same transaction — fhir.py itself is not edited.
- **Unmerge** restores BOTH patients from the stored snapshots
  exactly (body and version, so the rendered resources are
  byte-identical to their pre-merge state: source active again with
  no link, survivor back to its pre-merge fields) and moves the
  request to `reversed`. The cross-reference row is retained as the
  historical record of the merge.
- **Cross-reference resolution**: a merged source's identifiers keep
  resolving — `GET /empi/cross-reference?identifier=<value>` finds
  the merge by ANY identifier of either patient and returns the
  survivor reference.

Every step is audited in the same transaction via
app.audit.append_audit: `empi.merge.propose`, `empi.merge.approve`,
`empi.merge.reject`, `empi.merge.execute`, `empi.merge.unmerge`
(plus `empi.candidate.manual` for a manually created candidate).

Governed configuration: published `integration/empi` (seeded by
ensure_empi_defaults) declares the workflow switches
(requireSecondSteward / requireConfirm / autoMerge: false). The
endpoint enforcement reads those switches with the seeded defaults
as fallback, so turning a switch off is a publishing act, never a
code edit. All data is synthetic, like the rest of the system.
"""
import copy
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import (DateTime, ForeignKey, Integer, JSON, String, Text,
                        select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import (ConfigStatus, DedupCandidate, FhirResource, StudioConfig,
                     User, utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/empi", tags=["empi"])

EMPI_KEY = "empi"  # integration/empi
STATUSES = ("proposed", "approved", "executed", "rejected", "reversed")
FIELD_CHOICE_VALUES = ("source", "survivor")


# ---------------------------------------------------------------------------
# This module's own tables
# ---------------------------------------------------------------------------

class MergeRequest(Base):
    """One governed merge request — Phase 16 (BD-47).

    `field_choices` maps a top-level Patient field name to which side
    wins at execute time: "source" (take the source patient's value)
    or "survivor" (keep the survivor's). Fields absent from the map
    keep the survivor's value. The snapshots are also stored on the
    cross-reference row written at execute time; they are mirrored
    here so the request alone carries the full reversal record the
    CONTRACTS table shape names."""
    __tablename__ = "empi_merge_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_patient_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                                   index=True)
    survivor_patient_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                                     index=True)
    field_choices: Mapped[dict] = mapped_column(JSON, nullable=False,
                                                  default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="proposed", index=True)
    proposed_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    executed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reversed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    candidate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    survivor_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow, onupdate=utcnow)


class MergeCrossReference(Base):
    """The cross-reference retained for one executed merge — BD-47.

    Written BEFORE either patient is mutated, in the same transaction
    as the merge itself: both patients' identifier lists (so a merged
    source's MRNs keep resolving to the survivor) and the FULL
    pre-merge snapshot JSON of both patients (the unmerge source of
    truth). Retained after an unmerge as the historical record."""
    __tablename__ = "empi_merge_cross_references"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merge_id: Mapped[int] = mapped_column(
        ForeignKey("empi_merge_requests.id"), nullable=False, index=True)
    source_patient_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                                   index=True)
    survivor_patient_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                                     index=True)
    source_identifiers: Mapped[list] = mapped_column(JSON, nullable=False,
                                                     default=list)
    survivor_identifiers: Mapped[list] = mapped_column(JSON, nullable=False,
                                                       default=list)
    source_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False,
                                                    default=dict)
    survivor_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False,
                                                    default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Governed configuration (published integration/empi)
# ---------------------------------------------------------------------------

def default_empi_payload() -> dict:
    return {
        "name": "EMPI merge governance (synthetic defaults)",
        "description": ("Governed patient-merge workflow switches. "
                        "requireSecondSteward: the approver/rejecter "
                        "must differ from the proposer. requireConfirm: "
                        "execute needs an explicit confirm:true. "
                        "autoMerge is false and no code path merges "
                        "without an approved request."),
        "requireSecondSteward": True,
        "requireConfirm": True,
        "autoMerge": False,
    }


def _empi_config(session: Session) -> dict:
    cfg = published_config(session, "integration", EMPI_KEY)
    payload = dict(cfg.payload or {}) if cfg is not None else {}
    merged = default_empi_payload()
    merged.update({k: v for k, v in payload.items() if v is not None})
    return merged


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _patient_id_of(payload: dict, *names: str) -> str | None:
    """A patient id from any accepted field spelling; a full
    'Patient/<id>' reference is reduced to its id."""
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            return value.split("/", 1)[1] if value.startswith("Patient/") \
                else value
    return None


def _identifiers_of(body: dict) -> list[dict]:
    out = []
    for entry in (body or {}).get("identifier") or []:
        if isinstance(entry, dict):
            out.append({"system": entry.get("system"),
                        "value": entry.get("value")})
    return out


def _patient_row(session: Session, patient_id: str) -> FhirResource:
    return fhir._get_row("Patient", patient_id, session)


def _request_out(row: MergeRequest,
                 xref: MergeCrossReference | None = None) -> dict:
    out = {
        "id": row.id,
        # Task-spec field names, plus the CONTRACTS aliases.
        "source_patient_id": row.source_patient_id,
        "survivor_patient_id": row.survivor_patient_id,
        "source_id": row.source_patient_id,
        "survivor_id": row.survivor_patient_id,
        "source_ref": f"Patient/{row.source_patient_id}",
        "survivor_ref": f"Patient/{row.survivor_patient_id}",
        "field_choices": row.field_choices or {},
        "fieldChoices": row.field_choices or {},
        "status": row.status,
        "proposed_by": row.proposed_by,
        "steward": row.proposed_by,
        "approved_by": row.approved_by,
        "rejected_by": row.rejected_by,
        "executed_by": row.executed_by,
        "reversed_by": row.reversed_by,
        "candidate_id": row.candidate_id,
        "note": row.note,
        "executed_at": _aware(row.executed_at).isoformat()
        if row.executed_at else None,
        "created_at": _aware(row.created_at).isoformat()
        if row.created_at else None,
        "updated_at": _aware(row.updated_at).isoformat()
        if row.updated_at else None,
    }
    if xref is not None:
        out["cross_reference"] = _xref_out(xref)
    return out


def _xref_out(row: MergeCrossReference) -> dict:
    return {
        "id": row.id,
        "merge_id": row.merge_id,
        "source_patient_id": row.source_patient_id,
        "survivor_patient_id": row.survivor_patient_id,
        "source_ref": f"Patient/{row.source_patient_id}",
        "survivor_ref": f"Patient/{row.survivor_patient_id}",
        "source_identifiers": row.source_identifiers or [],
        "survivor_identifiers": row.survivor_identifiers or [],
        "source_snapshot": row.source_snapshot or {},
        "survivor_snapshot": row.survivor_snapshot or {},
        "created_at": _aware(row.created_at).isoformat()
        if row.created_at else None,
    }


def _candidate_out(row: DedupCandidate, origin: str = "dedup-scan") -> dict:
    return {
        "id": row.id,
        "candidate_id": row.id,
        "patient_a": row.patient_a,
        "patient_b": row.patient_b,
        "source_patient_id": row.patient_a,
        "survivor_patient_id": row.patient_b,
        "sourceId": row.patient_a,
        "survivorId": row.patient_b,
        "score": row.score,
        "reasons": row.reasons or {},
        "status": row.status,
        "run_id": row.run_id,
        "origin": origin,
        "created_at": _aware(row.created_at).isoformat()
        if row.created_at else None,
    }


def _get_request(session: Session, request_id: int) -> MergeRequest:
    row = session.scalars(select(MergeRequest).where(
        MergeRequest.id == request_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Merge request {request_id} not found")
    return row


def _xref_for(session: Session, merge_id: int) -> MergeCrossReference | None:
    return session.scalars(select(MergeCrossReference).where(
        MergeCrossReference.merge_id == merge_id)).first()


def _check_second_steward(config: dict, req: MergeRequest, user: User) -> None:
    if config.get("requireSecondSteward", True) \
            and user.username == req.proposed_by:
        raise HTTPException(
            status_code=403,
            detail=("A merge request must be approved or rejected by a "
                    "second steward — the proposer "
                    f"('{req.proposed_by}') cannot decide their own "
                    "request"))


def _validate_field_choices(raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422,
                            detail="'fieldChoices' must be an object mapping "
                                   "field name -> 'source'|'survivor'")
    out = {}
    for field, choice in raw.items():
        if not isinstance(field, str) or not field.strip():
            raise HTTPException(status_code=422,
                                detail="'fieldChoices' keys must be field names")
        if choice not in FIELD_CHOICE_VALUES:
            raise HTTPException(
                status_code=422,
                detail=f"'fieldChoices.{field}' must be 'source' or "
                       f"'survivor', got {choice!r}")
        out[field.strip()] = choice
    return out


def _resolve_pair(session: Session, payload: dict) -> tuple[str, str, int | None]:
    """(source_id, survivor_id, candidate_id) from a request/candidate
    body: an explicit pair in any accepted spelling, or a
    candidateId naming a Phase 14 DedupCandidate (whose patient_a is
    the source unless the caller names the direction explicitly)."""
    candidate_id = payload.get("candidateId") or payload.get("candidate_id")
    cand = None
    if candidate_id is not None:
        cand = session.scalars(select(DedupCandidate).where(
            DedupCandidate.id == candidate_id)).first()
        if cand is None:
            raise HTTPException(status_code=404,
                                detail=f"Dedup candidate {candidate_id} not found")
    source_id = _patient_id_of(payload, "sourceId", "source_id",
                               "sourcePatientId", "source_patient_id", "source")
    survivor_id = _patient_id_of(payload, "survivorId", "survivor_id",
                                 "survivorPatientId", "survivor_patient_id",
                                 "survivor")
    if cand is not None:
        source_id = source_id or cand.patient_a
        survivor_id = survivor_id or cand.patient_b
    if not source_id or not survivor_id:
        raise HTTPException(
            status_code=422,
            detail="Requires 'sourceId' and 'survivorId' (or a "
                   "'candidateId' naming a dedup candidate)")
    if source_id == survivor_id:
        raise HTTPException(status_code=422,
                            detail="Source and survivor must be different patients")
    return source_id, survivor_id, cand.id if cand is not None else None


# ---------------------------------------------------------------------------
# Candidates — Phase 14 dedup outputs + manual pair creation
# ---------------------------------------------------------------------------

@router.get("/candidates")
def list_candidates(status: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("empi.steward"))):
    """Merge candidates read from the Phase 14 dedup-scan outputs
    (the DedupCandidate table). Review items only — listing never
    merges."""
    stmt = select(DedupCandidate).order_by(DedupCandidate.score.desc(),
                                           DedupCandidate.id.asc())
    if status:
        stmt = stmt.where(DedupCandidate.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_candidate_out(r) for r in rows], "total": len(rows),
            "source": "dedup-scan",
            "note": ("Candidates are review items for a human steward. "
                     "Nothing here merges records; a merge needs a "
                     "proposed + approved MergeRequest.")}


@router.post("/candidates")
async def create_or_list_candidates(request: Request,
                                    session: Session = Depends(get_db),
                                    user: User = Depends(
                                        require_capability("empi.steward"))):
    """With a manual {sourceId, survivorId} pair: validate both
    patients exist and record the pair as a DedupCandidate review
    row (find-or-create on the sorted pair, exactly like the scan —
    so manual candidates join the same review surface). With no
    pair: return the latest dedup-scan candidates. Either way this
    endpoint NEVER merges."""
    payload = await _json_body(request)
    has_pair = any(payload.get(k) for k in (
        "sourceId", "survivorId", "source_id", "survivor_id",
        "sourcePatientId", "survivorPatientId", "candidateId", "candidate_id"))
    if not has_pair:
        stmt = select(DedupCandidate).order_by(DedupCandidate.score.desc(),
                                               DedupCandidate.id.asc())
        rows = session.scalars(stmt).all()
        return {"items": [_candidate_out(r) for r in rows],
                "total": len(rows), "source": "dedup-scan"}
    source_id, survivor_id, _ = _resolve_pair(session, payload)
    _patient_row(session, source_id)     # 404 when either patient is absent
    _patient_row(session, survivor_id)
    pair = tuple(sorted((source_id, survivor_id)))
    existing = session.scalars(select(DedupCandidate).where(
        DedupCandidate.patient_a == pair[0],
        DedupCandidate.patient_b == pair[1])).first()
    if existing is not None:
        out = _candidate_out(existing,
                             origin=(existing.reasons or {}).get("origin",
                                                                 "dedup-scan"))
        return {"candidate": out, "created": False, "merge_performed": False}
    row = DedupCandidate(patient_a=pair[0], patient_b=pair[1], score=100,
                         reasons={"origin": "manual",
                                  "source_patient_id": source_id,
                                  "survivor_patient_id": survivor_id},
                         run_id=None)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="empi.candidate.manual",
                 entity=f"DedupCandidate/{row.id}",
                 payload={"pair": [pair[0], pair[1]],
                          "source_patient_id": source_id,
                          "survivor_patient_id": survivor_id,
                          "merge_performed": False})
    session.commit()
    return JSONResponse(status_code=201,
                        content={"candidate": _candidate_out(row, "manual"),
                                 "created": True, "merge_performed": False})


# ---------------------------------------------------------------------------
# Merge requests — propose / approve / reject / execute / unmerge
# ---------------------------------------------------------------------------

@router.post("/requests", status_code=201)
async def propose_request(request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(
                              require_capability("empi.steward"))):
    """Propose a merge. Body: {sourceId, survivorId, fieldChoices}
    (or {candidateId, ...}). Both patients must exist, the source
    must not already be merged away, and no other open (proposed /
    approved) request may exist for the same ordered pair. Audited
    empi.merge.propose. Proposing mutates NO patient."""
    payload = await _json_body(request)
    source_id, survivor_id, candidate_id = _resolve_pair(session, payload)
    field_choices = _validate_field_choices(
        payload.get("fieldChoices", payload.get("field_choices")))
    source = _patient_row(session, source_id)
    _patient_row(session, survivor_id)
    if (source.body or {}).get("active") is False:
        raise HTTPException(
            status_code=409,
            detail=f"Patient/{source_id} is already inactive (merged) "
                   "and cannot be a merge source")
    open_req = session.scalars(select(MergeRequest).where(
        MergeRequest.source_patient_id == source_id,
        MergeRequest.survivor_patient_id == survivor_id,
        MergeRequest.status.in_(("proposed", "approved")))).first()
    if open_req is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Merge request {open_req.id} is already "
                   f"{open_req.status} for this pair")
    note = payload.get("note")
    req = MergeRequest(source_patient_id=source_id,
                       survivor_patient_id=survivor_id,
                       field_choices=field_choices, status="proposed",
                       proposed_by=user.username,
                       candidate_id=candidate_id,
                       note=note if isinstance(note, str) else None)
    session.add(req)
    session.flush()
    append_audit(session, actor=user.username, action="empi.merge.propose",
                 entity=f"MergeRequest/{req.id}",
                 payload={"source_patient_id": source_id,
                          "survivor_patient_id": survivor_id,
                          "field_choices": field_choices,
                          "candidate_id": candidate_id})
    session.commit()
    return _request_out(req)


@router.get("/requests")
def list_requests(status: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability("empi.steward"))):
    stmt = select(MergeRequest).order_by(MergeRequest.id.desc())
    if status:
        stmt = stmt.where(MergeRequest.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_request_out(r) for r in rows], "total": len(rows)}


@router.get("/requests/{request_id}")
def get_request(request_id: int, session: Session = Depends(get_db),
                user: User = Depends(require_capability("empi.steward"))):
    req = _get_request(session, request_id)
    return _request_out(req, _xref_for(session, req.id))


@router.post("/requests/{request_id}/approve")
async def approve_request(request_id: int, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(
                              require_capability("empi.steward"))):
    """Approve a proposed request — a SECOND steward action: the
    proposer cannot approve their own request (403). Audited
    empi.merge.approve."""
    req = _get_request(session, request_id)
    if req.status != "proposed":
        raise HTTPException(status_code=409,
                            detail=f"Merge request {request_id} is "
                                   f"{req.status}, not proposed")
    _check_second_steward(_empi_config(session), req, user)
    req.status = "approved"
    req.approved_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="empi.merge.approve",
                 entity=f"MergeRequest/{req.id}",
                 payload={"source_patient_id": req.source_patient_id,
                          "survivor_patient_id": req.survivor_patient_id,
                          "proposed_by": req.proposed_by})
    session.commit()
    return _request_out(req)


@router.post("/requests/{request_id}/reject")
async def reject_request(request_id: int, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(
                             require_capability("empi.steward"))):
    """Reject a proposed request — also a second-steward action
    (403 for the proposer). Audited empi.merge.reject."""
    payload = await _json_body(request)
    req = _get_request(session, request_id)
    if req.status != "proposed":
        raise HTTPException(status_code=409,
                            detail=f"Merge request {request_id} is "
                                   f"{req.status}, not proposed")
    _check_second_steward(_empi_config(session), req, user)
    req.status = "rejected"
    req.rejected_by = user.username
    note = payload.get("note")
    if isinstance(note, str) and note:
        req.note = note
    session.flush()
    append_audit(session, actor=user.username, action="empi.merge.reject",
                 entity=f"MergeRequest/{req.id}",
                 payload={"source_patient_id": req.source_patient_id,
                          "survivor_patient_id": req.survivor_patient_id,
                          "proposed_by": req.proposed_by,
                          "note": req.note})
    session.commit()
    return _request_out(req)


@router.post("/requests/{request_id}/execute")
async def execute_request(request_id: int, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(
                              require_capability("empi.steward"))):
    """Execute an APPROVED merge. Requires {confirm: true} (missing
    or false -> 422) and status approved (else 409).

    One transaction, in this order: (1) the cross-reference row with
    BOTH identifier lists and BOTH full pre-merge snapshots is
    written; (2) the survivor is updated per field_choices; (3) the
    source is set active:false + link replaced-by the survivor.
    Audited empi.merge.execute."""
    payload = await _json_body(request)
    config = _empi_config(session)
    if config.get("requireConfirm", True) and payload.get("confirm") is not True:
        raise HTTPException(
            status_code=422,
            detail="Execute requires explicit confirmation: "
                   "{'confirm': true}")
    req = _get_request(session, request_id)
    if req.status != "approved":
        raise HTTPException(
            status_code=409,
            detail=f"Merge request {request_id} is {req.status}; only an "
                   "approved request can be executed (no merge without "
                   "an approved request)")
    source = _patient_row(session, req.source_patient_id)
    survivor = _patient_row(session, req.survivor_patient_id)
    if _xref_for(session, req.id) is not None:
        raise HTTPException(status_code=409,
                            detail=f"Merge request {request_id} already "
                                   "has a cross-reference (already executed)")
    # (1) Snapshots + cross-reference FIRST, same transaction.
    source_snapshot = copy.deepcopy(fhir._render(source))
    survivor_snapshot = copy.deepcopy(fhir._render(survivor))
    xref = MergeCrossReference(
        merge_id=req.id,
        source_patient_id=req.source_patient_id,
        survivor_patient_id=req.survivor_patient_id,
        source_identifiers=_identifiers_of(source.body or {}),
        survivor_identifiers=_identifiers_of(survivor.body or {}),
        source_snapshot=source_snapshot,
        survivor_snapshot=survivor_snapshot)
    session.add(xref)
    session.flush()
    req.source_snapshot = source_snapshot
    req.survivor_snapshot = survivor_snapshot
    # (2) Survivor per field choices (fields not chosen keep the
    # survivor's value; a chosen field the source lacks is removed).
    merged = copy.deepcopy(survivor.body or {})
    source_body = source.body or {}
    for field, choice in (req.field_choices or {}).items():
        if choice == "source":
            if field in source_body:
                merged[field] = copy.deepcopy(source_body[field])
            else:
                merged.pop(field, None)
    merged["resourceType"] = "Patient"
    survivor.body = merged
    survivor.version += 1
    fhir._apply_projections(survivor, "Patient", merged,
                            req.survivor_patient_id)
    # (3) Source retired in place: inactive + replaced-by link. Its
    # identifiers stay on the source body AND in the cross-reference.
    source_new = copy.deepcopy(source_body)
    source_new["active"] = False
    source_new["link"] = [{
        "other": {"reference": f"Patient/{req.survivor_patient_id}"},
        "type": "replaced-by",
    }]
    source.body = source_new
    source.version += 1
    fhir._apply_projections(source, "Patient", source_new,
                            req.source_patient_id)
    req.status = "executed"
    req.executed_by = user.username
    req.executed_at = _now()
    session.flush()
    append_audit(session, actor=user.username, action="empi.merge.execute",
                 entity=f"MergeRequest/{req.id}",
                 payload={"source_patient_id": req.source_patient_id,
                          "survivor_patient_id": req.survivor_patient_id,
                          "field_choices": req.field_choices or {},
                          "cross_reference_id": xref.id,
                          "approved_by": req.approved_by})
    session.commit()
    return {"request": _request_out(req, xref),
            "cross_reference": _xref_out(xref),
            "survivor": fhir._render(survivor),
            "source": fhir._render(source)}


@router.post("/requests/{request_id}/unmerge")
async def unmerge_request(request_id: int, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(
                              require_capability("empi.steward"))):
    """Reverse an EXECUTED merge in full: both patients are restored
    from the cross-reference snapshots exactly (body AND version —
    the source is active again with its link removed, the survivor
    returns to its pre-merge fields). The request moves to
    `reversed`; the cross-reference row is retained as history.
    Audited empi.merge.unmerge."""
    req = _get_request(session, request_id)
    if req.status != "executed":
        raise HTTPException(
            status_code=409,
            detail=f"Merge request {request_id} is {req.status}; only an "
                   "executed merge can be unmerged")
    xref = _xref_for(session, req.id)
    if xref is None:
        raise HTTPException(
            status_code=409,
            detail=f"Merge request {request_id} has no cross-reference "
                   "snapshots to restore from")
    for patient_id, snapshot in (
            (req.source_patient_id, xref.source_snapshot),
            (req.survivor_patient_id, xref.survivor_snapshot)):
        row = _patient_row(session, patient_id)
        snap = snapshot or {}
        body = {k: copy.deepcopy(v) for k, v in snap.items() if k != "meta"}
        body["resourceType"] = "Patient"
        row.body = body
        version = (snap.get("meta") or {}).get("versionId")
        if version is not None:
            try:
                row.version = int(version)
            except (TypeError, ValueError):
                pass
        fhir._apply_projections(row, "Patient", body, patient_id)
    req.status = "reversed"
    req.reversed_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="empi.merge.unmerge",
                 entity=f"MergeRequest/{req.id}",
                 payload={"source_patient_id": req.source_patient_id,
                          "survivor_patient_id": req.survivor_patient_id,
                          "cross_reference_id": xref.id,
                          "executed_by": req.executed_by})
    session.commit()
    return {"request": _request_out(req, xref),
            "source": fhir._render(_patient_row(session, req.source_patient_id)),
            "survivor": fhir._render(
                _patient_row(session, req.survivor_patient_id))}


# ---------------------------------------------------------------------------
# Cross-reference resolution — a merged source's identifiers still resolve
# ---------------------------------------------------------------------------

def _identifier_matches(identifiers: list, wanted: str) -> bool:
    for entry in identifiers or []:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if value is not None and str(value) == wanted:
            return True
        system = entry.get("system")
        if system and value is not None \
                and f"{system}|{value}" == wanted:
            return True
    return False


@router.get("/cross-reference")
def resolve_cross_reference(identifier: str = Query(...),
                            session: Session = Depends(get_db),
                            user: User = Depends(
                                require_capability("empi.steward"))):
    """Resolve an identifier (value, or system|value) of EITHER side
    of an executed/reversed merge to its cross-reference: the source
    and survivor references plus both identifier lists. This is how a
    merged source's MRN still finds the surviving record."""
    wanted = identifier.strip()
    rows = session.scalars(select(MergeCrossReference).order_by(
        MergeCrossReference.id.asc())).all()
    items = []
    for xref in rows:
        if _identifier_matches(xref.source_identifiers or [], wanted) \
                or _identifier_matches(xref.survivor_identifiers or [], wanted) \
                or wanted in (xref.source_patient_id, xref.survivor_patient_id,
                              f"Patient/{xref.source_patient_id}",
                              f"Patient/{xref.survivor_patient_id}"):
            req = session.scalars(select(MergeRequest).where(
                MergeRequest.id == xref.merge_id)).first()
            item = _xref_out(xref)
            item["merge_status"] = req.status if req is not None else None
            items.append(item)
    return {"identifier": identifier, "items": items, "total": len(items),
            "resolved": len(items) > 0}


@router.get("/cross-reference/{patient_id}")
def cross_reference_for_patient(patient_id: str,
                                session: Session = Depends(get_db),
                                user: User = Depends(
                                    require_capability("empi.steward"))):
    pid = patient_id.split("/", 1)[1] if patient_id.startswith("Patient/") \
        else patient_id
    rows = session.scalars(select(MergeCrossReference).where(
        (MergeCrossReference.source_patient_id == pid)
        | (MergeCrossReference.survivor_patient_id == pid))).all()
    return {"patient_id": pid,
            "items": [_xref_out(r) for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Seeding — governed config
# ---------------------------------------------------------------------------

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


def ensure_empi_defaults(session: Session) -> None:
    """Seed the published integration/empi governance config
    (idempotent — existing versions are the truth)."""
    _seed_published(session, "integration", EMPI_KEY, default_empi_payload())
    session.commit()
