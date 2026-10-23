"""21 CFR Part 11 electronic signatures — Phase 16, WS-F2 (BD-46).

A signature here is a governed record, not a checkbox:

- ``SignatureRecord`` binds a signer to the EXACT record snapshot they
  reviewed: ``binding_hash = sha256(snapshotHash + signer + meaning +
  timestamp)`` where ``snapshotHash`` is the sha256 of the record's
  canonical snapshot (sorted-key JSON) at review time.
- ``POST /esign/sign`` recomputes the CURRENT record's snapshot hash
  through a registry of per-record-type snapshot functions and refuses
  **409** when it differs from the caller-supplied hash — the record
  changed since review, so the signature would attest to something the
  signer never saw. An unknown ``recordType`` (no snapshot function
  registered) is refused **422**: an unregistered type can never be
  signed, by construction.
- ``GET /esign/manifest/{recordType}/{recordId}`` returns the
  manifestation of the latest signature (signer, meaning, timestamp,
  binding) for display / print — the Part 11 §11.50 manifestation.

The PUBLISHED ``regulatory_pack/cfr-part-11`` pack drives the
requirements: while the pack is a DRAFT (the seeded state) its
parameters bind nothing; once a version is published,
``parameters.requireMeaning: true`` makes a signature without a
meaning a **422**. That flip is configuration, never a code branch
(the compliance-pack pattern, app.compliance / app.privacy).

Snapshot functions live with the owning module and register here:
app.research_ops registers ``ResearchStudy``, ``StudyProcedure`` and
``CoverageDetermination``. This module deliberately imports nothing
from research_ops at module scope (research_ops imports THIS module);
an unknown type triggers one lazy import attempt so registration
order can never strand a signable type.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .audit import append_audit
from .configstore import (CAPABILITY_DEFAULTS, list_config_versions,
                          published_config, role_allowed)
from .db import Base
from .models import ConfigStatus, StudioConfig, User, utcnow
from .security import get_current_user, get_db

router = APIRouter(prefix="/esign", tags=["esign"])

PART11_KEY = "cfr-part-11"

# Proposed capability defaults (coordinator wires these into
# app.configstore.CAPABILITY_DEFAULTS centrally; until then the local
# fallback below applies the SAME tuples so behaviour is identical
# before and after wiring, and an explicit published permissions
# override always wins either way).
ESIGN_SIGN_ROLES = ("admin", "configurator", "clinician")
_CAP_FALLBACKS = {"esign.sign": ESIGN_SIGN_ROLES}


# ---------------------------------------------------------------------------
# Capability gate (require_capability semantics + pre-wiring fallback)
# ---------------------------------------------------------------------------

def _cap_allowed(session: Session, user: User, capability: str) -> bool:
    cfg = published_config(session, "permissions", "role-matrix")
    if cfg is not None:
        overrides = ((cfg.payload or {}).get("capabilities") or {}).get(capability)
        if isinstance(overrides, dict) and user.role.value in overrides:
            return bool(overrides[user.role.value])
    if capability in CAPABILITY_DEFAULTS:
        return role_allowed(session, user.role, capability)
    return user.role.value in _CAP_FALLBACKS.get(capability, ())


def require_esign(capability: str):
    def dep(session: Session = Depends(get_db),
            user: User = Depends(get_current_user)) -> User:
        if not _cap_allowed(session, user, capability):
            raise HTTPException(
                status_code=403,
                detail=f"Role {user.role.value} lacks capability '{capability}'")
        return user
    return dep


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------

class SignatureRecord(Base):
    """One electronic signature: who signed what, with what meaning,
    when, and the cryptographic binding to the reviewed snapshot."""
    __tablename__ = "signature_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_type: Mapped[str] = mapped_column(String(80), nullable=False,
                                             index=True)
    record_id: Mapped[str] = mapped_column(String(200), nullable=False,
                                           index=True)
    signer_username: Mapped[str] = mapped_column(String(120), nullable=False)
    meaning: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                default=utcnow)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False,
                                              index=True)


# ---------------------------------------------------------------------------
# Snapshot registry + canonical hashing
# ---------------------------------------------------------------------------

SnapshotFn = Callable[[Session, str], dict | None]
_SNAPSHOT_REGISTRY: dict[str, SnapshotFn] = {}


def register_snapshot_fn(record_type: str, fn: SnapshotFn) -> None:
    """Register the canonical-snapshot function for one record type.
    Called by the owning module at import time (idempotent)."""
    _SNAPSHOT_REGISTRY[record_type] = fn


def registered_record_types() -> list[str]:
    return sorted(_SNAPSHOT_REGISTRY)


def canonical_snapshot_hash(snapshot: dict) -> str:
    canonical = json.dumps(snapshot, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_fn(record_type: str) -> SnapshotFn | None:
    fn = _SNAPSHOT_REGISTRY.get(record_type)
    if fn is None:
        # Lazy registration: the owning module registers on import;
        # importing it here keeps sign working regardless of the order
        # the coordinator mounts routers in.
        try:
            from . import research_ops  # noqa: F401
        except Exception:
            pass
        fn = _SNAPSHOT_REGISTRY.get(record_type)
    return fn


def compute_snapshot_hash(session: Session, record_type: str,
                          record_id: str) -> str | None:
    """The current canonical snapshot hash for one record, or None
    when the type is unregistered or the record does not exist."""
    fn = _snapshot_fn(record_type)
    if fn is None:
        return None
    snapshot = fn(session, str(record_id))
    if snapshot is None:
        return None
    return canonical_snapshot_hash(snapshot)


def binding_for(snapshot_hash: str, signer: str, meaning: str,
                signed_at_iso: str) -> str:
    """binding_hash = sha256(snapshotHash + signer + meaning + timestamp)."""
    material = f"{snapshot_hash}{signer}{meaning}{signed_at_iso}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Part 11 pack parameters (published pack drives requirements)
# ---------------------------------------------------------------------------

def part11_parameters(session: Session) -> dict:
    """Parameters of the PUBLISHED cfr-part-11 pack, or {} while no
    version is published (the seeded DRAFT binds nothing)."""
    cfg = published_config(session, "regulatory_pack", PART11_KEY)
    if cfg is None:
        return {}
    params = (cfg.payload or {}).get("parameters")
    return params if isinstance(params, dict) else {}


def part11_manifest_fields(session: Session) -> list[str]:
    params = part11_parameters(session)
    fields = params.get("manifestFields")
    if isinstance(fields, list) and fields:
        return [str(f) for f in fields]
    cfg = published_config(session, "regulatory_pack", PART11_KEY)
    if cfg is not None:
        top = (cfg.payload or {}).get("manifestFields")
        if isinstance(top, list) and top:
            return [str(f) for f in top]
    return ["signer", "meaning", "timestamp"]


# ---------------------------------------------------------------------------
# Signing (shared by the endpoint and by research_ops' governed actions)
# ---------------------------------------------------------------------------

def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def signature_out(row: SignatureRecord) -> dict:
    signed_at = _aware(row.signed_at)
    return {"id": row.id, "record_type": row.record_type,
            "record_id": row.record_id, "signer": row.signer_username,
            "signer_username": row.signer_username, "meaning": row.meaning,
            "signed_at": signed_at.isoformat() if signed_at else None,
            "snapshot_hash": row.snapshot_hash,
            "binding_hash": row.binding_hash}


def latest_signature(session: Session, record_type: str,
                     record_id: str) -> SignatureRecord | None:
    return session.scalars(
        select(SignatureRecord)
        .where(SignatureRecord.record_type == record_type,
               SignatureRecord.record_id == str(record_id))
        .order_by(SignatureRecord.id.desc())).first()


def sign_record(session: Session, *, record_type: str, record_id: str,
                signer: str, meaning: str | None,
                snapshot_hash: str) -> SignatureRecord:
    """Create one signature, enforcing the Part 11 rules. Raises
    HTTPException: 422 unknown type / missing meaning when the
    published pack requires it, 404 unknown record, 409 snapshot
    mismatch (the record changed since the signer reviewed it).
    The caller owns the transaction (flush only, no commit)."""
    record_id = str(record_id)
    fn = _snapshot_fn(record_type)
    if fn is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown recordType '{record_type}': no snapshot "
                   f"function is registered for it, so it cannot be signed")
    if not isinstance(snapshot_hash, str) or not snapshot_hash.strip():
        raise HTTPException(status_code=422,
                            detail="Signing requires 'snapshotHash' (the "
                                   "hash of the snapshot the signer reviewed)")
    snapshot_hash = snapshot_hash.strip()
    snapshot = fn(session, record_id)
    if snapshot is None:
        raise HTTPException(status_code=404,
                            detail=f"{record_type}/{record_id} not found")
    current_hash = canonical_snapshot_hash(snapshot)
    if current_hash != snapshot_hash:
        raise HTTPException(
            status_code=409,
            detail=f"{record_type}/{record_id} changed since the supplied "
                   "snapshot was taken (snapshot mismatch); re-review the "
                   "current record and sign its current snapshot hash")
    meaning = (meaning or "").strip() if isinstance(meaning, str) else ""
    params = part11_parameters(session)
    if params.get("requireMeaning") is True and not meaning:
        raise HTTPException(
            status_code=422,
            detail="The published 21 CFR Part 11 pack requires a signature "
                   "meaning (parameters.requireMeaning); supply 'meaning'")
    signed_at = datetime.now(timezone.utc)
    signed_at_iso = signed_at.isoformat()
    row = SignatureRecord(
        record_type=record_type, record_id=record_id, signer_username=signer,
        meaning=meaning, signed_at=signed_at, snapshot_hash=snapshot_hash,
        binding_hash=binding_for(snapshot_hash, signer, meaning, signed_at_iso))
    session.add(row)
    session.flush()
    append_audit(session, actor=signer, action="esign.sign",
                 entity=f"{record_type}/{record_id}",
                 payload={"signature_id": row.id, "meaning": meaning,
                          "snapshot_hash": snapshot_hash,
                          "binding_hash": row.binding_hash})
    return row


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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


@router.get("/snapshot/{record_type}/{record_id}")
def get_snapshot(record_type: str, record_id: str,
                 session: Session = Depends(get_db),
                 user: User = Depends(require_esign("esign.sign"))):
    """The current snapshot + its hash for one record — what a signer
    reviews before signing. 422 for an unregistered type, 404 for an
    unknown record."""
    fn = _snapshot_fn(record_type)
    if fn is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown recordType '{record_type}': no snapshot "
                   "function is registered for it")
    snapshot = fn(session, record_id)
    if snapshot is None:
        raise HTTPException(status_code=404,
                            detail=f"{record_type}/{record_id} not found")
    return {"record_type": record_type, "record_id": record_id,
            "snapshot": snapshot,
            "snapshot_hash": canonical_snapshot_hash(snapshot)}


@router.post("/sign", status_code=201)
async def sign(request: Request, session: Session = Depends(get_db),
               user: User = Depends(require_esign("esign.sign"))):
    """Sign one record: {recordType, recordId, meaning, snapshotHash}.
    Refuses 409 when the record changed since the snapshot the signer
    reviewed; 422 for an unknown recordType or (once the Part 11 pack
    is published with requireMeaning) a missing meaning."""
    payload = await _json_body(request)
    record_type = _field(payload, "recordType", "record_type")
    record_id = _field(payload, "recordId", "record_id")
    meaning = _field(payload, "meaning")
    snapshot_hash = _field(payload, "snapshotHash", "snapshot_hash")
    if not isinstance(record_type, str) or not record_type.strip():
        raise HTTPException(status_code=422, detail="'recordType' is required")
    if record_id is None or (isinstance(record_id, str)
                             and not record_id.strip()):
        raise HTTPException(status_code=422, detail="'recordId' is required")
    row = sign_record(session, record_type=record_type.strip(),
                      record_id=str(record_id), signer=user.username,
                      meaning=meaning if isinstance(meaning, str) else None,
                      snapshot_hash=snapshot_hash)
    session.commit()
    return signature_out(row)


@router.get("/manifest/{record_type}/{record_id}")
def get_manifest(record_type: str, record_id: str,
                 session: Session = Depends(get_db),
                 user: User = Depends(require_esign("esign.sign"))):
    """The manifestation of the latest signature on one record — the
    signer / meaning / timestamp / binding block shown next to the
    signed record and printed with it (Part 11 §11.50). 404 when the
    record carries no signature."""
    row = latest_signature(session, record_type, record_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No signature recorded for {record_type}/{record_id}")
    out = signature_out(row)
    fields = part11_manifest_fields(session)
    manifest = {"signer": out["signer"], "meaning": out["meaning"],
                "timestamp": out["signed_at"], "binding": out["binding_hash"]}
    return {"record_type": record_type, "record_id": record_id,
            "signature_id": row.id, "manifest_fields": fields,
            "manifestation": {k: manifest[k] for k in
                              ("signer", "meaning", "timestamp", "binding")},
            **out}


@router.get("/signatures/{record_type}/{record_id}")
def list_signatures(record_type: str, record_id: str,
                    session: Session = Depends(get_db),
                    user: User = Depends(require_esign("esign.sign"))):
    rows = session.scalars(
        select(SignatureRecord)
        .where(SignatureRecord.record_type == record_type,
               SignatureRecord.record_id == str(record_id))
        .order_by(SignatureRecord.id.asc())).all()
    return {"record_type": record_type, "record_id": record_id,
            "items": [signature_out(r) for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Seeds: the cfr-part-11 regulatory pack as a DRAFT
# ---------------------------------------------------------------------------

def default_part11_pack_payload() -> dict:
    return {
        "body": "FDA",
        "jurisdiction": "US",
        "parameters": {
            "requireMeaning": True,
            "manifestFields": ["signer", "meaning", "timestamp"],
        },
        "requirements": ["21 CFR Part 11", "21 CFR 11.10",
                         "21 CFR 11.50", "21 CFR 11.70"],
        "notes": ("21 CFR Part 11 electronic records / electronic "
                  "signatures baseline — DRAFT, legal attestation out "
                  "of scope. While this pack is a draft its parameters "
                  "bind nothing; publishing it makes requireMeaning "
                  "drive POST /esign/sign (a signature without a "
                  "meaning is refused 422) and names the manifestation "
                  "fields shown with every signature. Seeded as a draft "
                  "(Phase 16, WS-F2 / BD-46)."),
    }


def _seed_pack_draft(session: Session, key: str, payload: dict) -> None:
    """Insert-if-absent DRAFT pack — the compliance seeding pattern
    (app.compliance._seed_pack_draft)."""
    if list_config_versions(session, "regulatory_pack", key):
        return
    row = StudioConfig(kind="regulatory_pack", key=key, version=1,
                       status=ConfigStatus.draft, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:regulatory_pack/{key}@v1",
                 payload={"kind": "regulatory_pack", "key": key,
                          "version": 1, "seeded_default": True})


def ensure_esignature_defaults(session: Session) -> None:
    """Seed the cfr-part-11 regulatory pack as a DRAFT (idempotent).
    app.research_ops.ensure_research_defaults seeds the same pack via
    the same insert-if-absent helper, so either call order is safe."""
    _seed_pack_draft(session, PART11_KEY, default_part11_pack_payload())
    session.commit()
