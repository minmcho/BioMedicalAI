"""Phase 15 (B) — specimen lifecycle: aliquot/split derivation,
parent->child barcode chain + family chain of custody, add-on testing.

Builds ON the Phase 2/6B specimen spine (app.lab): the primary
specimen is the FHIR Specimen created at collection by the lab order
transition. This module adds, in its OWN tables (the protocols.py
own-tables precedent):

- derivation: aliquots / splits / subcultures / isolates as child
  FHIR Specimens (real `parent` link) tracked in LabSpecimenNode
  with full lineage, aliquot sequence, volume, container, status;
- barcodes: every specimen/aliquot gets its OWN barcode value in
  SpecimenBarcode (value, kind, entity ref) so the Phase 15
  universal barcode lookup can resolve them. Payloads follow the
  published barcode-standard pattern (pharmacy-ops
  `barcodeStandards`: PREFIX|field|field|check-digits, weighted
  mod-97 check digits from app.pharmacy_ops) extended for specimens
  by this module's own published config
  (terminology/specimen-lifecycle: specimenPrefix + separator);
- custody: every scan event appends an append-only
  SpecimenCustodyEvent (who/where/when/action) to that member's
  record; GET chain returns the whole family tree from ANY member;
  a scan whose required predecessor (per the published action
  sequence) is missing flags a custody BREAK, listed on the
  custody-breaks worklist;
- add-on testing: a test added onto an existing specimen, validated
  for status + volume sufficiency against the test's aliquot rules
  in the governed catalogue, creating a real ServiceRequest through
  the same FHIR store path the lab order endpoint uses
  (fhir.create_resource with the place_order body shape), linked to
  the existing specimen — the order logic is reused, never forked.

Behaviour differences are published Studio config
(terminology/specimen-lifecycle), never code constants beyond the
seed/fallback below.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir, lab
from .audit import append_audit
from .configstore import published_config
from .db import Base
from .models import FhirResource, User, utcnow
from .pharmacy_ops import _check_digits
from .security import get_db, require_capability

router = APIRouter(prefix="/lab", tags=["lab_specimens"])

SPECIMEN_CONFIG_KEY = "specimen-lifecycle"
DERIVATION_TYPES = ("aliquot", "split", "subculture", "isolate")
# Statuses from which derivation / add-on testing are refused.
TERMINAL_STATUSES = ("rejected", "disposed", "resulted", "verified",
                     "released", "final", "signed-out")


# ---------------------------------------------------------------------------
# Own tables
# ---------------------------------------------------------------------------

class SpecimenBarcode(Base):
    """One registered specimen barcode value — Phase 15 (B).

    The universal-lookup workstream resolves a scanned value through
    this table: `value` is the full payload string, `kind` the
    specimen kind (primary | aliquot | split | subculture |
    isolate), `entity_ref` the FHIR reference it resolves to
    (Specimen/<id>), `specimen_key` this module's node key.
    """
    __tablename__ = "specimen_barcodes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[str] = mapped_column(String(200), unique=True,
                                       nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                            index=True)
    specimen_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                              index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class LabSpecimenNode(Base):
    """One member of a specimen family (a primary specimen or one
    derived child). The clinical record is the FHIR Specimen; this
    row carries the lifecycle data FHIR does not: lineage, aliquot
    sequence, volume bookkeeping, container and derivation type."""
    __tablename__ = "lab_specimen_nodes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    specimen_key: Mapped[str] = mapped_column(String(120), unique=True,
                                              nullable=False, index=True)
    fhir_specimen_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                                  index=True)
    parent_key: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                   index=True)
    root_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    derivation_type: Mapped[str] = mapped_column(String(40), nullable=False,
                                                 default="primary")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    volume_ml: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    addon_consumed_ml: Mapped[float] = mapped_column(Float, nullable=False,
                                                     default=0.0)
    container: Mapped[str] = mapped_column(String(120), nullable=False,
                                           default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                         default="collected")
    order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    patient_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    organism_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    barcode_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class SpecimenCustodyEvent(Base):
    """One append-only chain-of-custody scan event for one specimen
    family member. `is_break` + `break_reason` are computed at scan
    time from the published action sequence and never rewritten."""
    __tablename__ = "specimen_custody_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    specimen_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                              index=True)
    root_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_break: Mapped[bool] = mapped_column(default=False, nullable=False)
    break_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class LabAddonRecord(Base):
    """One add-on test placed onto an existing specimen — the record
    linking specimen, new lab order and consumed volume."""
    __tablename__ = "lab_addon_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    specimen_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                              index=True)
    order_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    test_code: Mapped[str] = mapped_column(String(60), nullable=False)
    required_volume_ml: Mapped[float] = mapped_column(Float, nullable=False,
                                                      default=0.0)
    requested_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                              default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Published config (terminology/specimen-lifecycle)
# ---------------------------------------------------------------------------

DEFAULT_ACTIONS = ["collection", "transport", "receipt", "bench-transfer",
                   "storage", "send-out-dispatch", "disposal"]


def default_specimen_lifecycle_payload() -> dict:
    return {
        "name": "OpenCare specimen lifecycle (Phase 15)",
        # The scan-action vocabulary rides as `concepts` so the
        # document stays a valid, republishable terminology config.
        "concepts": [
            {"code": a, "display": a.replace("-", " ").title(), "active": True}
            for a in DEFAULT_ACTIONS
        ],
        # The required order of custody actions: a scan of action N
        # requires action N-1 already present in that member's chain.
        "actionSequence": list(DEFAULT_ACTIONS),
        # Specimen extension of the pharmacy-owned barcode standard
        # (terminology/pharmacy-ops barcodeStandards): same
        # PREFIX|field|field|check-digits shape, specimen prefix.
        "barcodeStandards": {"specimenPrefix": "OCSP1", "separator": "|"},
        "defaultVolumeMl": 10.0,
        "defaultContainer": "standard tube",
        "containers": ["standard tube", "edta tube", "serum separator tube",
                       "blood culture bottle", "sterile container",
                       "agar plate", "cryovial"],
    }


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    from .configstore import list_config_versions
    from .models import ConfigStatus, StudioConfig
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


def ensure_lab_specimens_defaults(session: Session) -> None:
    _seed_published(session, "terminology", SPECIMEN_CONFIG_KEY,
                    default_specimen_lifecycle_payload())
    session.commit()


def _lifecycle(session: Session) -> dict:
    cfg = published_config(session, "terminology", SPECIMEN_CONFIG_KEY)
    return (cfg.payload or {}) if cfg is not None \
        else default_specimen_lifecycle_payload()


def _action_sequence(cfg: dict) -> list[str]:
    seq = cfg.get("actionSequence")
    if isinstance(seq, list) and seq and all(isinstance(a, str) for a in seq):
        return list(seq)
    return list(DEFAULT_ACTIONS)


def _barcode_std(cfg: dict) -> tuple[str, str]:
    std = cfg.get("barcodeStandards") or {}
    return (std.get("specimenPrefix") or "OCSP1", std.get("separator") or "|")


def _make_barcode(cfg: dict, specimen_key: str, sequence: int,
                  kind: str) -> str:
    prefix, sep = _barcode_std(cfg)
    body = sep.join([prefix, specimen_key, f"{kind}-{sequence}"])
    return f"{body}{sep}{_check_digits(body)}"


# ---------------------------------------------------------------------------
# Node / specimen resolution
# ---------------------------------------------------------------------------

def _node_out(session: Session, node: LabSpecimenNode) -> dict:
    events = session.scalars(
        select(SpecimenCustodyEvent).where(
            SpecimenCustodyEvent.specimen_key == node.specimen_key)
        .order_by(SpecimenCustodyEvent.id.asc())).all()
    return {
        "specimen_key": node.specimen_key,
        "specimen_id": node.fhir_specimen_id,
        "entity_ref": f"Specimen/{node.fhir_specimen_id}",
        "parent_key": node.parent_key,
        "root_key": node.root_key,
        "derivation_type": node.derivation_type,
        "sequence": node.sequence,
        "volume_ml": node.volume_ml,
        "remaining_volume_ml": _remaining_volume(session, node),
        "container": node.container,
        "status": node.status,
        "order_id": node.order_id,
        "patient_ref": node.patient_ref,
        "organism_code": node.organism_code,
        "barcode": node.barcode_value,
        "custody": [_event_out(e) for e in events],
    }


def _event_out(event: SpecimenCustodyEvent) -> dict:
    return {"id": event.id, "specimen_key": event.specimen_key,
            "action": event.action, "actor": event.actor,
            "location": event.location, "note": event.note,
            "is_break": bool(event.is_break),
            "break_reason": event.break_reason,
            "at": event.created_at.isoformat() if event.created_at else None}


def _remaining_volume(session: Session, node: LabSpecimenNode) -> float:
    children = session.scalars(
        select(LabSpecimenNode).where(
            LabSpecimenNode.parent_key == node.specimen_key)).all()
    used = sum(c.volume_ml for c in children) + (node.addon_consumed_ml or 0.0)
    return round(node.volume_ml - used, 4)


def _register_barcode(session: Session, cfg: dict, node: LabSpecimenNode) -> str:
    value = _make_barcode(cfg, node.specimen_key, node.sequence,
                          node.derivation_type)
    existing = session.scalars(select(SpecimenBarcode).where(
        SpecimenBarcode.value == value)).first()
    if existing is None:
        session.add(SpecimenBarcode(
            value=value, kind=node.derivation_type,
            entity_ref=f"Specimen/{node.fhir_specimen_id}",
            specimen_key=node.specimen_key))
        session.flush()
    node.barcode_value = value
    return value


def _append_custody(session: Session, node: LabSpecimenNode, action: str,
                    actor: str, location: str | None = None,
                    note: str | None = None) -> SpecimenCustodyEvent:
    cfg = _lifecycle(session)
    sequence = _action_sequence(cfg)
    is_break, reason = False, None
    if action in sequence:
        idx = sequence.index(action)
        if idx > 0:
            predecessor = sequence[idx - 1]
            prior = {e.action for e in session.scalars(
                select(SpecimenCustodyEvent).where(
                    SpecimenCustodyEvent.specimen_key == node.specimen_key)
            ).all()}
            if predecessor not in prior:
                is_break = True
                reason = (f"Missing predecessor event '{predecessor}' "
                          f"before '{action}' in specimen "
                          f"{node.specimen_key}'s custody chain")
    event = SpecimenCustodyEvent(
        specimen_key=node.specimen_key, root_key=node.root_key,
        action=action, actor=actor, location=location, note=note,
        is_break=is_break, break_reason=reason)
    session.add(event)
    session.flush()
    return event


def _ensure_node(session: Session, specimen_row: FhirResource,
                 actor: str = "system") -> LabSpecimenNode:
    """Wrap an existing FHIR Specimen (the Phase 2/6B record) in a
    lifecycle node + primary barcode, idempotently. The root node's
    custody chain is seeded with the collection event the lab flow
    already performed."""
    node = session.scalars(select(LabSpecimenNode).where(
        LabSpecimenNode.fhir_specimen_id == specimen_row.resource_id)).first()
    if node is not None:
        return node
    cfg = _lifecycle(session)
    body = specimen_row.body or {}
    order_id = None
    for ref in body.get("request") or []:
        reference = ref.get("reference") if isinstance(ref, dict) else None
        if reference and reference.startswith("ServiceRequest/"):
            order_id = reference.split("/", 1)[1]
    volume = cfg.get("defaultVolumeMl")
    volume = float(volume) if isinstance(volume, (int, float)) \
        and not isinstance(volume, bool) else 10.0
    node = LabSpecimenNode(
        specimen_key=specimen_row.resource_id,
        fhir_specimen_id=specimen_row.resource_id,
        parent_key=None, root_key=specimen_row.resource_id,
        derivation_type="primary", sequence=0, volume_ml=volume,
        container=cfg.get("defaultContainer") or "standard tube",
        status=(body.get("status") or "collected"),
        order_id=order_id,
        patient_ref=(body.get("subject") or {}).get("reference"),
        created_by=actor)
    session.add(node)
    session.flush()
    _register_barcode(session, cfg, node)
    _append_custody(session, node, "collection", actor,
                    note="Primary specimen collected (lab order flow)")
    session.flush()
    return node


def _resolve_node(session: Session, identifier: str) -> LabSpecimenNode:
    """Resolve a family member by FHIR specimen id, node key, or
    barcode value (via SpecimenBarcode)."""
    node = session.scalars(select(LabSpecimenNode).where(
        LabSpecimenNode.specimen_key == identifier)).first()
    if node is not None:
        return node
    barcode = session.scalars(select(SpecimenBarcode).where(
        SpecimenBarcode.value == identifier)).first()
    if barcode is not None:
        node = session.scalars(select(LabSpecimenNode).where(
            LabSpecimenNode.specimen_key == barcode.specimen_key)).first()
        if node is not None:
            return node
    row = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Specimen",
        FhirResource.resource_id == identifier)).first()
    if row is not None:
        return _ensure_node(session, row)
    raise HTTPException(status_code=404,
                        detail=f"Specimen '{identifier}' not found")


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


# ---------------------------------------------------------------------------
# Derivation (aliquots / splits / subcultures / isolates)
# ---------------------------------------------------------------------------

def _derive(session: Session, user: User, specimen_id: str,
            payload: dict) -> dict:
    """Derive child specimens from an existing lab specimen. Each
    child is a real FHIR Specimen with a `parent` link, its own
    lifecycle node (lineage, sequence, volume, container, status)
    and its own registered barcode. Subculture/isolate derivations
    carry the microbiology linkage (organism, validated against the
    published micro-organisms terminology when supplied)."""
    parent = _resolve_node(session, specimen_id)
    if parent.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(f"Specimen {specimen_id} is '{parent.status}'; "
                    "derivation is not possible from a terminal specimen"))
    dtype = payload.get("type") or payload.get("derivation_type") or "aliquot"
    if dtype not in DERIVATION_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"'type' must be one of {list(DERIVATION_TYPES)}")
    count = payload.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int) \
            or count < 1 or count > 20:
        raise HTTPException(status_code=422,
                            detail="'count' must be an integer in 1..20")
    volume = payload.get("volume_ml")
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) \
            or float(volume) <= 0:
        raise HTTPException(status_code=422,
                            detail="'volume_ml' must be a positive number "
                                   "(the volume of EACH derived child)")
    volume = float(volume)
    container = payload.get("container") or parent.container
    organism = payload.get("organism_code")
    if organism is not None:
        cfg_org = published_config(session, "terminology", lab.ORGANISMS_KEY)
        known = {c.get("code") for c in (cfg_org.payload or {}).get("concepts")
                 or [] if isinstance(c, dict)} if cfg_org else set()
        if organism not in known:
            raise HTTPException(
                status_code=422,
                detail=(f"Organism '{organism}' is not in the published "
                        "micro-organisms terminology"))
    remaining = _remaining_volume(session, parent)
    if count * volume > remaining + 1e-9:
        raise HTTPException(
            status_code=422,
            detail=(f"Insufficient remaining volume on specimen "
                    f"{specimen_id}: {remaining} mL available, "
                    f"{count * volume} mL requested"))
    parent_row = fhir._get_row("Specimen", parent.fhir_specimen_id, session)
    parent_body = parent_row.body or {}
    existing_children = session.scalars(select(LabSpecimenNode).where(
        LabSpecimenNode.parent_key == parent.specimen_key)).all()
    next_seq = (max((c.sequence for c in existing_children), default=0) + 1)
    total_specimens = len(session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Specimen")).all())
    cfg = _lifecycle(session)
    children = []
    for i in range(count):
        seq = next_seq + i
        child_id = str(uuid.uuid4())
        accession = (f"ACC-{datetime.now(timezone.utc):%Y%m%d}-"
                     f"{total_specimens + 1 + i:04d}-{dtype[:2].upper()}{seq}")
        child_body = {
            "resourceType": "Specimen", "id": child_id,
            "status": parent.status if parent.status not in (
                "resulted", "verified", "released") else "collected",
            "accessionIdentifier": {"system": "urn:opencare:accession",
                                    "value": accession},
            "type": parent_body.get("type") or {"text": "Blood specimen"},
            "subject": parent_body.get("subject"),
            "request": parent_body.get("request") or [],
            "parent": [{"reference": f"Specimen/{parent.fhir_specimen_id}"}],
            "collection": {"collectedDateTime": lab._now_iso()},
            "extension": [
                {"url": "urn:opencare:extension/derivation-type",
                 "valueString": dtype},
                {"url": "urn:opencare:extension/parent-specimen",
                 "valueString": parent.fhir_specimen_id},
            ],
        }
        if organism:
            child_body["extension"].append(
                {"url": "urn:opencare:extension/organism",
                 "valueString": str(organism)})
        rendered = fhir.create_resource("Specimen", child_body, session, user)
        node = LabSpecimenNode(
            specimen_key=rendered["id"], fhir_specimen_id=rendered["id"],
            parent_key=parent.specimen_key, root_key=parent.root_key,
            derivation_type=dtype, sequence=seq, volume_ml=volume,
            container=str(container),
            status=child_body["status"], order_id=parent.order_id,
            patient_ref=parent.patient_ref,
            organism_code=str(organism) if organism else None,
            created_by=user.username)
        session.add(node)
        session.flush()
        _register_barcode(session, cfg, node)
        session.flush()
        append_audit(session, actor=user.username,
                     action="lab.specimen.derive",
                     entity=f"Specimen/{rendered['id']}",
                     payload={"parent": parent.specimen_key,
                              "type": dtype, "sequence": seq,
                              "volume_ml": volume, "container": str(container),
                              "organism_code": organism,
                              "barcode": node.barcode_value})
        children.append(node)
    session.commit()
    return {"parent": _node_out(session, parent),
            "children": [_node_out(session, c) for c in children],
            "total": len(children)}


@router.post("/specimens/{specimen_id}/derive", status_code=201)
async def derive_specimens(specimen_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    return _derive(session, user, specimen_id, payload)


@router.post("/specimens/{specimen_id}/subculture", status_code=201)
async def subculture_specimen(specimen_id: str, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability("lab.work"))):
    """Microbiology convenience route over derive: a subculture (or
    isolate, with type='isolate') child linked to its source
    specimen, with the organism linkage recorded."""
    payload = await _json_body(request)
    payload.setdefault("type", "subculture")
    return _derive(session, user, specimen_id, payload)


# ---------------------------------------------------------------------------
# Custody scans + family chain + breaks worklist
# ---------------------------------------------------------------------------

@router.post("/specimens/scan", status_code=201)
async def scan_specimen(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    """Record one custody scan for any family member (identified by
    specimen id or barcode value). The action must be in the
    published action vocabulary; a scan whose required predecessor
    is missing flags a custody break on the event (and therefore on
    the custody-breaks worklist) — the event is still recorded: the
    chain shows what actually happened, breaks included."""
    payload = await _json_body(request)
    identifier = payload.get("identifier") or payload.get("specimen_id") \
        or payload.get("barcode")
    if not identifier:
        raise HTTPException(status_code=422,
                            detail="'identifier' (specimen id or barcode) "
                                   "is required")
    action = payload.get("action")
    cfg = _lifecycle(session)
    if action not in _action_sequence(cfg):
        raise HTTPException(
            status_code=422,
            detail=(f"Unknown custody action '{action}'; the published "
                    f"vocabulary is {_action_sequence(cfg)}"))
    node = _resolve_node(session, str(identifier))
    event = _append_custody(session, node, str(action), user.username,
                            location=payload.get("location"),
                            note=payload.get("note"))
    if action == "disposal":
        node.status = "disposed"
    elif action == "storage":
        node.status = "stored"
    session.flush()
    append_audit(session, actor=user.username, action="lab.specimen.scan",
                 entity=f"Specimen/{node.fhir_specimen_id}",
                 payload={"action": action, "location": payload.get("location"),
                          "is_break": bool(event.is_break),
                          "break_reason": event.break_reason})
    session.commit()
    return {"event": _event_out(event), "specimen": _node_out(session, node)}


@router.get("/specimens/{identifier}/chain")
def specimen_chain(identifier: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    """The full family chain for ANY member (by id or barcode): the
    parent->child tree with every member's custody timeline and any
    custody breaks in the family."""
    member = _resolve_node(session, identifier)
    members = session.scalars(select(LabSpecimenNode).where(
        LabSpecimenNode.root_key == member.root_key)
        .order_by(LabSpecimenNode.id.asc())).all()
    out_members = [_node_out(session, m) for m in members]
    breaks = []
    for m in out_members:
        for event in m["custody"]:
            if event["is_break"]:
                breaks.append({"specimen_key": m["specimen_key"],
                               **event})

    def tree(key):
        node_out = next(m for m in out_members if m["specimen_key"] == key)
        kids = [m for m in out_members if m["parent_key"] == key]
        return {**node_out,
                "children": [tree(k["specimen_key"]) for k in kids]}

    return {"root_key": member.root_key,
            "requested_member": member.specimen_key,
            "members": out_members, "total": len(out_members),
            "tree": tree(member.root_key), "breaks": breaks}


@router.get("/specimens/custody-breaks")
def custody_breaks(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    """The custody-break worklist: every flagged family with the
    member, action and reason for each break."""
    events = session.scalars(select(SpecimenCustodyEvent).where(
        SpecimenCustodyEvent.is_break.is_(True))
        .order_by(SpecimenCustodyEvent.id.asc())).all()
    items = [{"root_key": e.root_key, "specimen_key": e.specimen_key,
              "action": e.action, "reason": e.break_reason,
              "actor": e.actor, "location": e.location,
              "at": e.created_at.isoformat() if e.created_at else None}
             for e in events]
    families = sorted({e.root_key for e in events})
    return {"items": items, "total": len(items), "families": families,
            "family_count": len(families)}


@router.get("/specimen-barcodes")
def list_specimen_barcodes(session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.read"))):
    """The specimen barcode registry (SpecimenBarcode): every value
    the universal lookup can resolve to a specimen."""
    rows = session.scalars(select(SpecimenBarcode).order_by(
        SpecimenBarcode.id.asc())).all()
    return {"items": [{"value": r.value, "kind": r.kind,
                       "entity_ref": r.entity_ref,
                       "specimen_key": r.specimen_key} for r in rows],
            "total": len(rows)}


# ---------------------------------------------------------------------------
# Add-on testing
# ---------------------------------------------------------------------------

def _required_volume(concept: dict) -> float:
    rules = concept.get("aliquotRules")
    if isinstance(rules, dict):
        volume = rules.get("volumeMl")
        count = rules.get("count")
        if isinstance(volume, (int, float)) and not isinstance(volume, bool):
            n = count if isinstance(count, int) and not isinstance(count, bool) \
                and count >= 1 else 1
            return float(volume) * n
    return 5.0


def record_addon_consumption(session: Session, node: LabSpecimenNode,
                             concept: dict, order_id: str,
                             requested_by: str) -> LabAddonRecord:
    """LAB-D11 (app.lab_tat): the shared volume-consumption step of
    the add-on path — the required volume per the test's aliquot
    rules is checked against the node's remaining volume and, when
    sufficient, consumed (addon_consumed_ml) with a LabAddonRecord
    linking specimen, order and volume. Insufficient volume raises
    the same named 422 the add-on endpoint raises, BEFORE any
    write. The caller owns order creation and the commit."""
    required = _required_volume(concept)
    remaining = _remaining_volume(session, node)
    if required > remaining + 1e-9:
        raise HTTPException(
            status_code=422,
            detail=(f"Insufficient volume for add-on "
                    f"'{concept.get('code')}': requires {required} mL "
                    f"per its aliquot rules, specimen has {remaining} "
                    "mL remaining"))
    node.addon_consumed_ml = (node.addon_consumed_ml or 0.0) + required
    record = LabAddonRecord(specimen_key=node.specimen_key,
                            order_id=order_id,
                            test_code=str(concept.get("code")),
                            required_volume_ml=required,
                            requested_by=requested_by)
    session.add(record)
    session.flush()
    return record


@router.post("/specimens/{specimen_id}/add-tests", status_code=201)
async def add_on_test(specimen_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.work"))):
    """Add a test onto an existing specimen: validates the specimen
    status and the remaining-volume sufficiency against the test's
    aliquot rules in the published catalogue, then creates the
    add-on lab order through the same FHIR create path + body shape
    as POST /lab/orders (app.lab.place_order), linked to the
    existing specimen and stamped as an add-on."""
    payload = await _json_body(request)
    test_code = payload.get("test_code") or payload.get("code")
    if not test_code:
        raise HTTPException(status_code=422,
                            detail="'test_code' is required")
    concept = lab._concept(session, str(test_code))
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown lab code '{test_code}': not in the "
                   "published lab catalog")
    if concept.get("active") is False:
        raise HTTPException(status_code=422,
                            detail=f"Lab code '{test_code}' is inactive")
    if concept.get("panel"):
        raise HTTPException(
            status_code=422,
            detail="Add-on testing names a single test, not a panel; "
                   "order panels through the lab order flow")
    node = _resolve_node(session, specimen_id)
    if node.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(f"Specimen {specimen_id} is '{node.status}'; add-on "
                    "testing requires a live specimen"))
    required = _required_volume(concept)
    remaining = _remaining_volume(session, node)
    if required > remaining + 1e-9:
        raise HTTPException(
            status_code=422,
            detail=(f"Insufficient volume for add-on '{test_code}': "
                    f"requires {required} mL per its aliquot rules, "
                    f"specimen has {remaining} mL remaining"))
    if not node.patient_ref:
        raise HTTPException(status_code=422,
                            detail="Specimen has no patient reference; "
                                   "an add-on order cannot be placed")
    order_body = {
        "resourceType": "ServiceRequest",
        "status": "collected",
        "intent": "order",
        "category": [{"coding": [{
            "system": "http://snomed.info/sct", "code": "108252007",
            "display": "Laboratory procedure"}]}],
        "code": {"coding": [{"system": lab.LOINC, "code": str(test_code),
                             "display": concept.get("display")}],
                 "text": concept.get("display")},
        "subject": {"reference": node.patient_ref},
        "authoredOn": lab._now_iso(),
        "requester": {"display": user.username},
        "specimen": [{"reference": f"Specimen/{node.fhir_specimen_id}"}],
        "extension": [
            {"url": "urn:opencare:extension/add-on", "valueBoolean": True},
            {"url": "urn:opencare:extension/add-on-specimen",
             "valueString": node.fhir_specimen_id},
        ],
    }
    rendered = fhir.create_resource("ServiceRequest", order_body, session, user)
    node.addon_consumed_ml = (node.addon_consumed_ml or 0.0) + required
    record = LabAddonRecord(specimen_key=node.specimen_key,
                            order_id=rendered["id"],
                            test_code=str(test_code),
                            required_volume_ml=required,
                            requested_by=user.username)
    session.add(record)
    session.flush()
    append_audit(session, actor=user.username, action="lab.specimen.addon",
                 entity=f"ServiceRequest/{rendered['id']}",
                 payload={"specimen_key": node.specimen_key,
                          "test_code": str(test_code),
                          "required_volume_ml": required})
    session.commit()
    return {"addon": {"id": record.id, "specimen_key": node.specimen_key,
                      "test_code": str(test_code),
                      "required_volume_ml": required,
                      "order_id": rendered["id"]},
            "order": rendered,
            "specimen": _node_out(session, node)}
