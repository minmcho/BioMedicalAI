"""LAB-D3/D4/D5 — Pre-analytical depth (Lab Depth pass, worker W2).

The laboratory AROUND collection and dispatch, built on the existing
spines — nothing here forks them:

- **D3 accessioning & transport.** The laboratory accession is minted
  AT LABORATORY RECEIPT, not at collection. The identifier the legacy
  collection transition (app.lab) stamps on the Specimen is carried
  forward as the specimen's COLLECTION ID (preserved on the Specimen
  `identifier` list + the receipt record, resolvable through
  GET /lab/preanalytical/resolve/{identifier}); at receipt the
  specimen gains its accession and BOTH identifiers stay resolvable
  forever — no reference is orphaned. Receipt happens on a
  scan-to-receive queue per RECEIVING BENCH (the bench registry is
  app.lab_compendium's terminology/lab-benches — consumed, never
  redefined) with a governed acceptability checklist whose items map
  onto the EXISTING governed rejection criteria (the lab catalog's
  `rejectionCriteria`): a failed item routes the order through the
  EXISTING rejection transition in app.lab (coded criterion +
  rejection history + recollect Task) — there is no second rejection
  system. Transport rules per specimen type are governed Studio
  config: tube eligibility incl. a never-tube list, temperature
  condition, maximum transit time. Transport custody scans record
  the transport facts and EXCEPTION-FLAG violations (never-tube,
  tube-ineligible, temperature breach, transit over max) on the
  specimen record itself (FHIR extension) and on the transport
  record.

- **D4 storage & retention.** A governed storage-location hierarchy
  (nested locations), a per-specimen-type retention schedule
  (governed hold days) computing a disposal due date per stored
  specimen, an audited disposal workflow, and retrieval logging —
  every retrieval of a stored specimen (add-on, repeat, delta
  lookup, …) is an append-only event with a purpose. Temperature
  linkage: a storage location may name a device-registry sensor
  (app.devices / models.Device); excursion-flagged readings in the
  registry recorded while a specimen was stored in that unit flag
  the affected specimens. HONEST BOUND: the device registry's
  readings are recorded/simulated inputs (app.devices' own honesty
  rule — every reading carries a source label); this module LINKS
  stored specimens to those records and flags excursions from them.
  It does not claim a live sensor feed, and every surface here
  labels the feed accordingly.

- **D5 send-out depth (catalog-as-contract).** The published
  terminology/sendout-labs entries (app.lab's payload, extended in
  this pass) carry the contract per test: a stability table +
  shipping temperature ENFORCED at manifest packing (out-of-
  stability or wrong-temperature packing refuses BY NAME with zero
  dispatch), split-before-transport instructions carried on the
  manifest, necessary-information attachments gating dispatch
  (a missing attachment is a named refusal), an expected report
  window driving an overdue chase list, the performing-laboratory
  identity stamped onto the received report, the FDA-cleared vs LDT
  classification carried and shown, and a send-out
  rejection/unsuitable RETURN path (the returned specimen re-enters
  receipt with a coded reason). Packing is the gate; dispatch writes
  the IDENTICAL send-out state the existing machinery uses (through
  app.lab_benches' own helpers) so the existing queue / TAT /
  receive-result surfaces see depth-dispatched orders uniformly.
  One deliberate difference: the depth dispatch accepts ACCESSIONED
  (received) orders — an unaccessioned specimen cannot be packed —
  where the legacy endpoint dispatches straight from `collected`.

Configure, don't fork — governed Studio config:
  terminology/lab-preanalytical  (this module: receipt checklist,
    transport rules, storage locations, retention schedule,
    retrieval purposes, send-out return reasons)
  terminology/sendout-labs       (app.lab: labs + per-test contracts)
  terminology/lab-benches        (app.lab_compendium: bench registry)
  terminology/lab-catalog        (app.lab: rejectionCriteria — the
    checklist's coded reasons resolve against it)

Storage: own tables only (receipts, transport records, storage
records, retrieval events, excursion flags, manifests, returns).
No columns are added to any existing table, so no ensure hook is
owed. Synthetic data only.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir, lab, lab_specimens
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import ConfigStatus, Device, DeviceReading, FhirResource, StudioConfig, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/lab/preanalytical", tags=["lab-preanalytical"])

PREANALYTICAL_KEY = "lab-preanalytical"
COLLECTION_ID_SYSTEM = "urn:opencare:collection-id"
ACCESSIONED_EXTENSION = "urn:opencare:extension/accession-received"
TRANSPORT_EXCEPTIONS_EXTENSION = "urn:opencare:extension/transport-exceptions"
PERFORMING_LAB_EXTENSION = "urn:opencare:extension/performing-laboratory"
RETRIEVAL_PURPOSES = ("addon", "repeat", "delta-lookup", "send-out", "review")
TRANSPORT_METHODS = ("pneumatic-tube", "courier", "hand-carry", "internal-porter")
TEMPERATURE_CONDITIONS = ("ambient", "refrigerated", "frozen", "body-temperature")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_ts(raw) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Own tables
# ---------------------------------------------------------------------------

class LabReceiptRecord(Base):
    """One laboratory receipt (accessioning) event for one specimen.
    `collection_id` is the identifier minted at collection (preserved
    verbatim); `accession` is minted HERE, at receipt, on acceptance.
    A rejected receipt carries the coded criterion the existing
    rejection flow recorded. A specimen returned from a send-out gets
    a NEW receipt record on re-receipt; the old one is marked
    `returned`."""
    __tablename__ = "lab_receipt_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    specimen_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    bench_code: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    collection_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    accession: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="accepted")
    checklist: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rejection_criterion: Mapped[str | None] = mapped_column(String(80), nullable=True)
    transport_exceptions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    return_reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    received_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LabTransportRecord(Base):
    """One transport custody scan's facts + computed exception flags:
    method, observed temperature condition, transit minutes against
    the governed rule for the specimen type, and the named exception
    codes (never-tube / tube-ineligible / temperature-breach /
    transit-over-max)."""
    __tablename__ = "lab_transport_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    specimen_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    temperature_condition: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    transit_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    exceptions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LabStorageRecord(Base):
    """One storage placement of one specimen: governed location,
    retention due date computed from the governed schedule at store
    time, and the disposal outcome when the workflow completes."""
    __tablename__ = "lab_storage_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    specimen_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    location_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="stored")
    stored_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    stored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    disposal_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disposed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    disposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disposal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class LabRetrievalEvent(Base):
    """One retrieval of a stored specimen — append-only, with the
    governed purpose (add-on / repeat / delta lookup / …)."""
    __tablename__ = "lab_retrieval_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    specimen_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    storage_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LabStorageExcursion(Base):
    """One excursion flag: a device-registry reading flagged
    `excursion` that was recorded while this specimen was stored in
    the location linked to that sensor. Materialised by the
    evaluate endpoint; never invented — the reading id is the
    evidence."""
    __tablename__ = "lab_storage_excursions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    specimen_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    storage_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location_code: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    device_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    reading_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    flagged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LabSendoutManifest(Base):
    """One packed send-out manifest (the D5 contract gate's output).
    Packing validates EVERY order (stability, shipping temperature,
    necessary-information attachments) before this row exists — a
    refusal writes nothing and dispatches nothing. Dispatch stamps
    `dispatched_at` and the expected report-by instant computed from
    the contract's report window."""
    __tablename__ = "lab_sendout_manifests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    manifest_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    lab_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    order_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    shipping_temperature: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    split_instructions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attachments: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="packed")
    packed_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    packed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_report_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LabSendoutReturn(Base):
    """One send-out rejection / unsuitable-specimen return: the
    reference laboratory sent the specimen back with a coded reason;
    the specimen re-enters the receiving queue."""
    __tablename__ = "lab_sendout_returns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    specimen_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    lab_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_display: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    re_received: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Governed config (terminology/lab-preanalytical)
# ---------------------------------------------------------------------------

def default_preanalytical_payload() -> dict:
    return {
        "name": "OpenCare laboratory pre-analytical rules (Lab Depth D3/D4/D5)",
        "concepts": [
            {"code": c, "display": c.replace("-", " ").title(), "active": True}
            for c in ("label-legible", "container-correct", "volume-adequate",
                      "no-haemolysis", "no-clot", "temperature-in-range",
                      "within-stability")
        ],
        # Receipt acceptability checklist. Each item maps to a code in
        # the lab catalog's governed rejectionCriteria: when an item
        # fails at receipt, the order is rejected through app.lab's
        # EXISTING rejection transition with that coded criterion.
        "receiptChecklist": [
            {"code": "label-legible", "display": "Label legible and matches requisition",
             "criterionCode": "mislabelled", "required": True},
            {"code": "container-correct", "display": "Correct container / additive",
             "criterionCode": "wrong-container", "required": True},
            {"code": "volume-adequate", "display": "Volume adequate for the tests ordered",
             "criterionCode": "insufficient", "required": True},
            {"code": "no-haemolysis", "display": "No invalidating haemolysis",
             "criterionCode": "hemolysed", "required": True},
            {"code": "no-clot", "display": "No clot in an anticoagulated tube",
             "criterionCode": "clotted", "required": True},
            {"code": "temperature-in-range", "display": "Transport temperature in range",
             "criterionCode": "expired", "required": True},
            {"code": "within-stability", "display": "Within specimen stability window",
             "criterionCode": "expired", "required": True},
        ],
        # Transport rules per specimen type (the Specimen type text).
        # neverTube specimens must be hand-carried (the Hopkins
        # suspected-TB sputum precedent); a tubed never-tube specimen
        # is exception-flagged, never silently accepted.
        "transportRules": [
            {"specimenType": "Blood specimen", "tubeEligible": True,
             "neverTube": False, "temperatureCondition": "ambient",
             "maxTransitMinutes": 240},
            {"specimenType": "Sputum specimen", "tubeEligible": False,
             "neverTube": True, "temperatureCondition": "ambient",
             "maxTransitMinutes": 120},
            {"specimenType": "Urine specimen", "tubeEligible": True,
             "neverTube": False, "temperatureCondition": "refrigerated",
             "maxTransitMinutes": 240},
            {"specimenType": "Cerebrospinal fluid specimen", "tubeEligible": False,
             "neverTube": True, "temperatureCondition": "ambient",
             "maxTransitMinutes": 60},
        ],
        "defaultTransportRule": {"tubeEligible": True, "neverTube": False,
                                 "temperatureCondition": "ambient",
                                 "maxTransitMinutes": 480},
        # Storage location hierarchy (nested via parentCode). A
        # location may link a device-registry temperature sensor
        # (deviceKey) — the D4 excursion linkage reads THAT registry.
        "storageLocations": [
            {"code": "retained-store", "display": "Retained specimen store",
             "parentCode": None, "temperatureCondition": "refrigerated",
             "deviceKey": None},
            {"code": "fridge-retained-a", "display": "Retained fridge A",
             "parentCode": "retained-store",
             "temperatureCondition": "refrigerated",
             "deviceKey": "fridge-lab-1"},
            {"code": "freezer-retained-a", "display": "Retained freezer A (-80)",
             "parentCode": "retained-store",
             "temperatureCondition": "frozen", "deviceKey": None},
            {"code": "ambient-shelf-a", "display": "Ambient retained shelf A",
             "parentCode": "retained-store",
             "temperatureCondition": "ambient", "deviceKey": None},
        ],
        # Retention schedule: hold days per specimen type from the
        # storage date; disposal due = stored date + hold days.
        "retentionSchedule": [
            {"specimenType": "Blood specimen", "holdDays": 7},
            {"specimenType": "Urine specimen", "holdDays": 2},
            {"specimenType": "Sputum specimen", "holdDays": 3},
            {"specimenType": "Cerebrospinal fluid specimen", "holdDays": 7},
        ],
        "defaultHoldDays": 7,
        "retrievalPurposes": list(RETRIEVAL_PURPOSES),
        # Coded reasons a reference laboratory returns a send-out
        # specimen as rejected / unsuitable.
        "sendoutReturnReasons": [
            {"code": "stability-expired", "display": "Received beyond stability limit"},
            {"code": "unsuitable-container", "display": "Unsuitable container"},
            {"code": "insufficient-volume", "display": "Insufficient volume"},
            {"code": "temperature-excursion", "display": "Temperature excursion in transit"},
            {"code": "mislabelled", "display": "Mislabelled at reference laboratory receipt"},
        ],
    }


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
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


def ensure_lab_preanalytical_defaults(session: Session) -> None:
    _seed_published(session, "terminology", PREANALYTICAL_KEY,
                    default_preanalytical_payload())
    session.commit()


def _config(session: Session) -> dict:
    cfg = published_config(session, "terminology", PREANALYTICAL_KEY)
    return (cfg.payload or {}) if cfg is not None \
        else default_preanalytical_payload()


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


class _ReplayRequest:
    """A request stand-in for calling an existing endpoint function
    in-process with a JSON payload (the app.lab transition and the
    app.lab_benches send-out receive-result are REUSED, not forked)."""

    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


# ---------------------------------------------------------------------------
# Shared resolution helpers
# ---------------------------------------------------------------------------

def _specimen_type(specimen: FhirResource) -> str:
    return ((specimen.body or {}).get("type") or {}).get("text") or ""


def _collection_id_of(specimen: FhirResource) -> str | None:
    """The specimen's collection ID: the identifier preserved at
    receipt, or — before receipt — the identifier the collection
    transition minted (which receipt then preserves)."""
    body = specimen.body or {}
    for ident in body.get("identifier") or []:
        if isinstance(ident, dict) and ident.get("system") == COLLECTION_ID_SYSTEM:
            return ident.get("value")
    return (body.get("accessionIdentifier") or {}).get("value")


def _accession_of(specimen: FhirResource) -> str | None:
    """The LABORATORY accession: present only once receipt has minted
    it (the accession-received extension is the receipt stamp)."""
    body = specimen.body or {}
    stamped = any(isinstance(e, dict) and e.get("url") == ACCESSIONED_EXTENSION
                  for e in body.get("extension") or [])
    if not stamped:
        return None
    return (body.get("accessionIdentifier") or {}).get("value")


def _find_specimen(session: Session, identifier: str) -> FhirResource | None:
    """Resolve ANY specimen identifier: FHIR id, laboratory accession,
    collection ID, or a registered specimen barcode."""
    row = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Specimen",
        FhirResource.resource_id == identifier)).first()
    if row is not None:
        return row
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Specimen")).all()
    for candidate in rows:
        body = candidate.body or {}
        if (body.get("accessionIdentifier") or {}).get("value") == identifier:
            return candidate
        for ident in body.get("identifier") or []:
            if isinstance(ident, dict) and ident.get("value") == identifier:
                return candidate
    barcode = session.scalars(select(lab_specimens.SpecimenBarcode).where(
        lab_specimens.SpecimenBarcode.value == identifier)).first()
    if barcode is not None:
        return session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Specimen",
            FhirResource.resource_id == barcode.specimen_key)).first()
    return None


def _order_for_specimen(session: Session, specimen: FhirResource) -> FhirResource | None:
    for ref in (specimen.body or {}).get("request") or []:
        reference = ref.get("reference") if isinstance(ref, dict) else None
        if reference and reference.startswith("ServiceRequest/"):
            return session.scalars(select(FhirResource).where(
                FhirResource.resource_type == "ServiceRequest",
                FhirResource.resource_id == reference.split("/", 1)[1])).first()
    return None


def _transport_rule(session: Session, specimen_type: str) -> dict:
    cfg = _config(session)
    for rule in cfg.get("transportRules") or []:
        if isinstance(rule, dict) and rule.get("specimenType") == specimen_type:
            return rule
    return dict(cfg.get("defaultTransportRule") or {})


def _bench_or_404(session: Session, bench_code: str) -> dict:
    from . import lab_compendium
    return lab_compendium._bench(session, bench_code)


def _receipt_out(record: LabReceiptRecord) -> dict:
    return {"id": record.id, "specimen_key": record.specimen_key,
            "order_id": record.order_id, "bench_code": record.bench_code,
            "collection_id": record.collection_id,
            "accession": record.accession, "status": record.status,
            "checklist": record.checklist,
            "rejection_criterion": record.rejection_criterion,
            "transport_exceptions": record.transport_exceptions or [],
            "return_reason_code": record.return_reason_code,
            "received_by": record.received_by,
            "received_at": record.received_at.isoformat() if record.received_at else None}


def _latest_receipt(session: Session, specimen_key: str) -> LabReceiptRecord | None:
    return session.scalars(select(LabReceiptRecord).where(
        LabReceiptRecord.specimen_key == specimen_key)
        .order_by(LabReceiptRecord.id.desc())).first()


def _accepted_receipt(session: Session, specimen_key: str) -> LabReceiptRecord | None:
    return session.scalars(select(LabReceiptRecord).where(
        LabReceiptRecord.specimen_key == specimen_key,
        LabReceiptRecord.status == "accepted")
        .order_by(LabReceiptRecord.id.desc())).first()


@router.get("/config")
def preanalytical_config(session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.read"))):
    """The governed pre-analytical rules in force (checklist,
    transport rules, storage hierarchy, retention schedule)."""
    cfg = _config(session)
    return {"config_key": f"terminology/{PREANALYTICAL_KEY}",
            "receipt_checklist": cfg.get("receiptChecklist") or [],
            "transport_rules": cfg.get("transportRules") or [],
            "default_transport_rule": cfg.get("defaultTransportRule"),
            "storage_locations": cfg.get("storageLocations") or [],
            "retention_schedule": cfg.get("retentionSchedule") or [],
            "default_hold_days": cfg.get("defaultHoldDays"),
            "retrieval_purposes": cfg.get("retrievalPurposes") or [],
            "sendout_return_reasons": cfg.get("sendoutReturnReasons") or []}


@router.get("/resolve/{identifier}")
def resolve_identifier(identifier: str, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.read"))):
    """Resolve any specimen identifier — FHIR id, collection ID,
    laboratory accession, or barcode — to the specimen with BOTH the
    collection ID and (once received) the accession shown. This is
    what keeps every pre-D3 identifier resolvable."""
    specimen = _find_specimen(session, identifier)
    if specimen is None:
        raise HTTPException(status_code=404,
                            detail=f"No specimen resolves identifier '{identifier}'")
    receipt = _latest_receipt(session, specimen.resource_id)
    return {"specimen_id": specimen.resource_id,
            "collection_id": _collection_id_of(specimen),
            "accession": _accession_of(specimen),
            "received": receipt is not None and receipt.status == "accepted",
            "status": (specimen.body or {}).get("status"),
            "specimen_type": _specimen_type(specimen),
            "receipt": _receipt_out(receipt) if receipt else None}


# ---------------------------------------------------------------------------
# D3 — receiving queue, scan-to-receive accessioning, transport scans
# ---------------------------------------------------------------------------

def _queue_items(session: Session, bench_code: str | None) -> list[dict]:
    """Specimens collected but not yet accepted at receipt (plus
    send-out returns awaiting re-receipt), optionally for one
    receiving bench (the bench the order's test routes to)."""
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "ServiceRequest")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = []
    for order in rows:
        if (order.body or {}).get("status") != "collected":
            continue
        specimen = lab._specimen_row(session, order)
        if specimen is None:
            continue
        if _accepted_receipt(session, specimen.resource_id) is not None:
            continue
        code = lab._code_of(order.body or {})
        concept = lab._concept(session, code) if code else None
        bench = (concept or {}).get("bench") or ""
        if bench_code and bench != bench_code:
            continue
        returned = session.scalars(select(LabSendoutReturn).where(
            LabSendoutReturn.order_id == order.resource_id,
            LabSendoutReturn.re_received.is_(False))
            .order_by(LabSendoutReturn.id.desc())).first()
        custody = lab._custody(order, specimen)
        items.append({
            "order_id": order.resource_id, "code": code,
            "display": (concept or {}).get("display"),
            "bench_code": bench,
            "specimen_id": specimen.resource_id,
            "specimen_type": _specimen_type(specimen),
            "collection_id": _collection_id_of(specimen),
            "accession": None,
            "patient_ref": (order.body or {}).get("subject", {}).get("reference"),
            "collected_at": custody.get("collected_at"),
            "returned_from_sendout": returned is not None,
            "return_reason_code": returned.reason_code if returned else None,
            "return_reason_display": returned.reason_display if returned else None,
        })
    return items


@router.get("/receiving-queue")
def receiving_queue(bench: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("lab.read"))):
    """The scan-to-receive accessioning queue. With ?bench=, only
    that receiving bench's queue (the bench must exist in the
    published terminology/lab-benches registry)."""
    if bench:
        _bench_or_404(session, bench)
    items = _queue_items(session, bench)
    return {"items": items, "total": len(items), "bench": bench,
            "checklist": _config(session).get("receiptChecklist") or []}


def _evaluate_transport(session: Session, specimen: FhirResource,
                        method: str, temperature_condition: str,
                        at: datetime) -> tuple[dict, list[dict]]:
    """Compute the transport exceptions for one specimen movement
    against the governed rule for its type."""
    rule = _transport_rule(session, _specimen_type(specimen))
    exceptions: list[dict] = []
    if method == "pneumatic-tube":
        if rule.get("neverTube"):
            exceptions.append({
                "code": "never-tube",
                "detail": (f"{_specimen_type(specimen)} is on the governed "
                           "never-tube list but was sent by pneumatic tube")})
        elif not rule.get("tubeEligible", True):
            exceptions.append({
                "code": "tube-ineligible",
                "detail": (f"{_specimen_type(specimen)} is not tube-eligible "
                           "under the governed transport rules")})
    expected_temp = rule.get("temperatureCondition")
    if temperature_condition and expected_temp \
            and temperature_condition != expected_temp:
        exceptions.append({
            "code": "temperature-breach",
            "detail": (f"Transported at '{temperature_condition}' but the "
                       f"governed condition for {_specimen_type(specimen)} "
                       f"is '{expected_temp}'")})
    collected = _parse_ts(((specimen.body or {}).get("collection") or {})
                          .get("collectedDateTime"))
    transit_minutes = None
    if collected is not None:
        transit_minutes = round((at - collected).total_seconds() / 60.0, 2)
        max_transit = rule.get("maxTransitMinutes")
        if isinstance(max_transit, (int, float)) \
                and transit_minutes > float(max_transit):
            exceptions.append({
                "code": "transit-over-max",
                "detail": (f"Transit time {transit_minutes} min exceeds the "
                           f"governed maximum {max_transit} min for "
                           f"{_specimen_type(specimen)}")})
    return {"rule": rule, "transit_minutes": transit_minutes,
            "exceptions": exceptions}, exceptions


def _stamp_transport_exceptions(session: Session, specimen: FhirResource,
                                exceptions: list[dict]) -> None:
    """Exception flags live ON the specimen record (FHIR extension),
    appended — never overwritten."""
    if not exceptions:
        return
    body = dict(specimen.body or {})
    exts = list(body.get("extension") or [])
    for exc in exceptions:
        exts.append({"url": TRANSPORT_EXCEPTIONS_EXTENSION,
                     "valueString": f"{exc['code']}: {exc['detail']}"})
    body["extension"] = exts
    specimen.body = body
    specimen.version += 1
    session.flush()


@router.post("/transport-scan", status_code=201)
async def transport_scan(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.work"))):
    """Record one transport custody scan with its transport facts
    (method + observed temperature condition). The governed rule for
    the specimen type is evaluated and any violation is
    EXCEPTION-FLAGGED on the transport record and on the specimen
    record itself. The custody event itself rides the existing
    lab_specimens chain (action 'transport')."""
    payload = await _json_body(request)
    identifier = payload.get("specimen_id") or payload.get("identifier") \
        or payload.get("barcode")
    if not identifier:
        raise HTTPException(status_code=422,
                            detail="'specimen_id' (or barcode) is required")
    method = payload.get("method")
    if method not in TRANSPORT_METHODS:
        raise HTTPException(
            status_code=422,
            detail=f"'method' must be one of {list(TRANSPORT_METHODS)}")
    temperature = payload.get("temperature_condition") or ""
    if temperature and temperature not in TEMPERATURE_CONDITIONS:
        raise HTTPException(
            status_code=422,
            detail=f"'temperature_condition' must be one of "
                   f"{list(TEMPERATURE_CONDITIONS)}")
    at = _parse_ts(payload.get("at")) or _now()
    specimen = _find_specimen(session, str(identifier))
    if specimen is None:
        raise HTTPException(status_code=404,
                            detail=f"Specimen '{identifier}' not found")
    evaluation, exceptions = _evaluate_transport(
        session, specimen, str(method), str(temperature), at)
    node = lab_specimens._resolve_node(session, specimen.resource_id)
    lab_specimens._append_custody(session, node, "transport", user.username,
                                  location=payload.get("location"),
                                  note=payload.get("note"))
    record = LabTransportRecord(
        specimen_key=specimen.resource_id, method=str(method),
        temperature_condition=str(temperature),
        transit_minutes=evaluation["transit_minutes"],
        exceptions=exceptions, actor=user.username,
        note=payload.get("note"))
    session.add(record)
    _stamp_transport_exceptions(session, specimen, exceptions)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.preanalytical.transport_scan",
                 entity=f"Specimen/{specimen.resource_id}",
                 payload={"method": method,
                          "temperature_condition": temperature,
                          "transit_minutes": evaluation["transit_minutes"],
                          "exceptions": [e["code"] for e in exceptions]})
    session.commit()
    return {"transport": {"id": record.id, "specimen_key": record.specimen_key,
                          "method": record.method,
                          "temperature_condition": record.temperature_condition,
                          "transit_minutes": record.transit_minutes,
                          "exceptions": exceptions,
                          "rule": evaluation["rule"]},
            "specimen_id": specimen.resource_id,
            "collection_id": _collection_id_of(specimen)}


@router.get("/transport-exceptions")
def transport_exceptions(session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.read"))):
    """The transport exception worklist: every flagged movement with
    its named violation(s)."""
    records = session.scalars(select(LabTransportRecord)
                              .order_by(LabTransportRecord.id.asc())).all()
    items = []
    for record in records:
        if record.exceptions:
            items.append({"transport_id": record.id,
                          "specimen_key": record.specimen_key,
                          "method": record.method,
                          "temperature_condition": record.temperature_condition,
                          "transit_minutes": record.transit_minutes,
                          "exceptions": record.exceptions,
                          "actor": record.actor,
                          "at": record.created_at.isoformat()
                          if record.created_at else None})
    return {"items": items, "total": len(items)}


def _mint_accession(session: Session, at: datetime) -> str:
    """Mint a laboratory accession in the house format, unique
    against every accession already carried by a Specimen or issued
    by a receipt record."""
    prefix = f"ACC-{at:%Y%m%d}-"
    taken = set()
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Specimen")).all():
        value = ((row.body or {}).get("accessionIdentifier") or {}).get("value")
        if isinstance(value, str) and value.startswith(prefix):
            taken.add(value)
    for record in session.scalars(select(LabReceiptRecord).where(
            LabReceiptRecord.accession.is_not(None))).all():
        if record.accession and record.accession.startswith(prefix):
            taken.add(record.accession)
    seq = len(taken) + 1
    while f"{prefix}{seq:04d}" in taken:
        seq += 1
    return f"{prefix}{seq:04d}"


@router.post("/receive", status_code=201)
async def receive_specimen(request: Request, session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    """Scan-to-receive accessioning. The payload names the specimen
    (id / collection ID / barcode), the receiving bench, and the
    governed checklist results [{code, passed}].

    - Every required checklist item must be reported.
    - A FAILED item routes the order into the EXISTING rejection
      flow (app.lab's transition with the item's governed coded
      criterion — rejection history + recollect Task included); the
      receipt record closes as `rejected` with that criterion and NO
      accession is minted.
    - On acceptance the order moves through the existing `received`
      transition, the collection-minted identifier is preserved as
      the collection ID, and the laboratory ACCESSION IS MINTED
      HERE — the specimen gains its accession at receipt.
    """
    payload = await _json_body(request)
    identifier = payload.get("specimen_id") or payload.get("identifier") \
        or payload.get("barcode")
    if not identifier:
        raise HTTPException(status_code=422,
                            detail="'specimen_id' (or barcode) is required")
    bench_code = payload.get("bench_code")
    if not bench_code:
        raise HTTPException(status_code=422, detail="'bench_code' is required")
    _bench_or_404(session, str(bench_code))
    specimen = _find_specimen(session, str(identifier))
    if specimen is None:
        raise HTTPException(status_code=404,
                            detail=f"Specimen '{identifier}' not found")
    if _accepted_receipt(session, specimen.resource_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Specimen {specimen.resource_id} has already been "
                   "received and accessioned")
    order = _order_for_specimen(session, specimen)
    if order is None:
        raise HTTPException(status_code=422,
                            detail="Specimen has no lab order; it cannot be "
                                   "accessioned at receipt")
    if (order.body or {}).get("status") != "collected":
        raise HTTPException(
            status_code=409,
            detail=(f"Order {order.resource_id} is "
                    f"'{(order.body or {}).get('status')}'; receipt requires "
                    "a collected specimen awaiting receipt"))
    cfg = _config(session)
    checklist_cfg = {item.get("code"): item
                     for item in cfg.get("receiptChecklist") or []
                     if isinstance(item, dict)}
    results = payload.get("checklist")
    if not isinstance(results, list) or not results:
        raise HTTPException(status_code=422,
                            detail="'checklist' must be a non-empty list of "
                                   "{code, passed} results")
    reported: dict[str, bool] = {}
    for entry in results:
        if not isinstance(entry, dict) or entry.get("code") not in checklist_cfg:
            raise HTTPException(
                status_code=422,
                detail=(f"Unknown checklist item "
                        f"'{entry.get('code') if isinstance(entry, dict) else entry}'; "
                        f"governed items are {sorted(checklist_cfg)}"))
        if not isinstance(entry.get("passed"), bool):
            raise HTTPException(
                status_code=422,
                detail=f"Checklist item '{entry['code']}' requires a boolean 'passed'")
        reported[entry["code"]] = entry["passed"]
    missing = [code for code, item in checklist_cfg.items()
               if item.get("required", True) and code not in reported]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Receipt checklist is incomplete; missing required "
                   f"items: {missing}")
    failed = [code for code, passed in reported.items() if not passed]
    collection_id = _collection_id_of(specimen)
    returned = session.scalars(select(LabSendoutReturn).where(
        LabSendoutReturn.order_id == order.resource_id,
        LabSendoutReturn.re_received.is_(False))
        .order_by(LabSendoutReturn.id.desc())).first()

    if failed:
        # Route into the EXISTING rejection flow with the failed
        # item's governed coded criterion.
        chosen = payload.get("reject_item") or failed[0]
        if chosen not in failed:
            raise HTTPException(
                status_code=422,
                detail=f"'reject_item' must name a failed checklist item; "
                       f"failed items are {failed}")
        criterion = checklist_cfg[chosen].get("criterionCode")
        reason = payload.get("reason") or (
            f"Receipt checklist failed at {bench_code}: "
            f"{checklist_cfg[chosen].get('display')}")
        outcome = await lab.transition_order(
            order.resource_id,
            _ReplayRequest({"to_status": "rejected", "reason": reason,
                            "reason_code": criterion}),
            session, user)
        record = LabReceiptRecord(
            specimen_key=specimen.resource_id, order_id=order.resource_id,
            bench_code=str(bench_code), collection_id=collection_id,
            accession=None, status="rejected", checklist=results,
            rejection_criterion=criterion,
            return_reason_code=returned.reason_code if returned else None,
            received_by=user.username, received_at=_now())
        session.add(record)
        if returned is not None:
            returned.re_received = True
        session.flush()
        append_audit(session, actor=user.username,
                     action="lab.preanalytical.receipt_rejected",
                     entity=f"Specimen/{specimen.resource_id}",
                     payload={"bench_code": bench_code,
                              "criterion": criterion,
                              "failed_items": failed,
                              "collection_id": collection_id})
        session.commit()
        return {"receipt": _receipt_out(record),
                "rejected": True,
                "order_status": outcome["order"].get("status"),
                "rejection": {"criterion_code": criterion,
                              "failed_items": failed}}

    # Acceptance: existing `received` transition first (it stamps
    # receivedDateTime through the lab machinery), then mint the
    # accession HERE and preserve the collection ID.
    await lab.transition_order(
        order.resource_id, _ReplayRequest({"to_status": "received"}),
        session, user)
    specimen = fhir._get_row("Specimen", specimen.resource_id, session)
    body = dict(specimen.body or {})
    accession = _mint_accession(session, _now())
    identifiers = [i for i in body.get("identifier") or []
                   if not (isinstance(i, dict)
                           and i.get("system") == COLLECTION_ID_SYSTEM)]
    if collection_id:
        identifiers.append({"system": COLLECTION_ID_SYSTEM,
                            "value": collection_id})
    body["identifier"] = identifiers
    body["accessionIdentifier"] = {"system": "urn:opencare:accession",
                                   "value": accession}
    exts = [e for e in body.get("extension") or []
            if not (isinstance(e, dict) and e.get("url") == ACCESSIONED_EXTENSION)]
    exts.append({"url": ACCESSIONED_EXTENSION, "valueDateTime": _now_iso()})
    body["extension"] = exts
    specimen.body = body
    specimen.version += 1
    session.flush()
    node = lab_specimens._resolve_node(session, specimen.resource_id)
    lab_specimens._append_custody(session, node, "receipt", user.username,
                                  location=str(bench_code),
                                  note="Accessioned at laboratory receipt")
    # Transport exceptions known so far ride onto the receipt record.
    known_exceptions: list[dict] = []
    for trec in session.scalars(select(LabTransportRecord).where(
            LabTransportRecord.specimen_key == specimen.resource_id)).all():
        known_exceptions.extend(trec.exceptions or [])
    record = LabReceiptRecord(
        specimen_key=specimen.resource_id, order_id=order.resource_id,
        bench_code=str(bench_code), collection_id=collection_id,
        accession=accession, status="accepted", checklist=results,
        transport_exceptions=known_exceptions,
        return_reason_code=returned.reason_code if returned else None,
        received_by=user.username, received_at=_now())
    session.add(record)
    if returned is not None:
        returned.re_received = True
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.preanalytical.receipt",
                 entity=f"Specimen/{specimen.resource_id}",
                 payload={"bench_code": bench_code, "accession": accession,
                          "collection_id": collection_id,
                          "transport_exceptions":
                          [e["code"] for e in known_exceptions]})
    session.commit()
    return {"receipt": _receipt_out(record), "rejected": False,
            "specimen_id": specimen.resource_id,
            "collection_id": collection_id, "accession": accession}


# ---------------------------------------------------------------------------
# D4 — storage locations, retention, disposal, retrieval, excursions
# ---------------------------------------------------------------------------

def _location(session: Session, code: str) -> dict:
    for entry in _config(session).get("storageLocations") or []:
        if isinstance(entry, dict) and entry.get("code") == code:
            return entry
    raise HTTPException(
        status_code=422,
        detail=(f"Storage location '{code}' is not in the governed "
                f"storage hierarchy (terminology/{PREANALYTICAL_KEY})"))


def _hold_days(session: Session, specimen_type: str) -> int:
    cfg = _config(session)
    for entry in cfg.get("retentionSchedule") or []:
        if isinstance(entry, dict) and entry.get("specimenType") == specimen_type:
            days = entry.get("holdDays")
            if isinstance(days, (int, float)) and not isinstance(days, bool):
                return int(days)
    days = cfg.get("defaultHoldDays")
    return int(days) if isinstance(days, (int, float)) \
        and not isinstance(days, bool) else 7


def _storage_out(session: Session, record: LabStorageRecord) -> dict:
    flags = session.scalars(select(LabStorageExcursion).where(
        LabStorageExcursion.storage_record_id == record.id)).all()
    return {"id": record.id, "specimen_key": record.specimen_key,
            "location_code": record.location_code, "status": record.status,
            "stored_by": record.stored_by,
            "stored_at": record.stored_at.isoformat() if record.stored_at else None,
            "disposal_due": record.disposal_due.isoformat() if record.disposal_due else None,
            "disposal_due_date": record.disposal_due.date().isoformat()
            if record.disposal_due else None,
            "disposed_by": record.disposed_by,
            "disposed_at": record.disposed_at.isoformat() if record.disposed_at else None,
            "disposal_reason": record.disposal_reason,
            "excursion_flags": [{"id": f.id, "device_key": f.device_key,
                                 "reading_id": f.reading_id,
                                 "detail": f.detail} for f in flags]}


def _active_storage(session: Session, specimen_key: str) -> LabStorageRecord | None:
    return session.scalars(select(LabStorageRecord).where(
        LabStorageRecord.specimen_key == specimen_key,
        LabStorageRecord.status == "stored")
        .order_by(LabStorageRecord.id.desc())).first()


@router.get("/storage/locations")
def storage_locations(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    """The governed storage hierarchy as a tree (nested locations),
    each leaf carrying its linked temperature sensor when one is
    governed."""
    entries = [e for e in _config(session).get("storageLocations") or []
               if isinstance(e, dict)]
    counts: dict[str, int] = {}
    for record in session.scalars(select(LabStorageRecord).where(
            LabStorageRecord.status == "stored")).all():
        counts[record.location_code] = counts.get(record.location_code, 0) + 1

    def node(entry):
        kids = [node(e) for e in entries if e.get("parentCode") == entry.get("code")]
        return {"code": entry.get("code"), "display": entry.get("display"),
                "parent_code": entry.get("parentCode"),
                "temperature_condition": entry.get("temperatureCondition"),
                "device_key": entry.get("deviceKey"),
                "stored_count": counts.get(entry.get("code"), 0),
                "children": kids}

    roots = [node(e) for e in entries if not e.get("parentCode")]
    return {"locations": roots, "total_locations": len(entries)}


@router.post("/storage/store", status_code=201)
async def store_specimen(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.work"))):
    """Place a received specimen into a governed storage location.
    The retention due date is computed NOW from the governed
    per-type schedule. The custody chain gains its `storage` event
    through the existing lab_specimens machinery."""
    payload = await _json_body(request)
    identifier = payload.get("specimen_id") or payload.get("identifier") \
        or payload.get("barcode")
    location_code = payload.get("location_code")
    if not identifier:
        raise HTTPException(status_code=422, detail="'specimen_id' is required")
    if not location_code:
        raise HTTPException(status_code=422, detail="'location_code' is required")
    location = _location(session, str(location_code))
    specimen = _find_specimen(session, str(identifier))
    if specimen is None:
        raise HTTPException(status_code=404,
                            detail=f"Specimen '{identifier}' not found")
    if _accepted_receipt(session, specimen.resource_id) is None:
        raise HTTPException(
            status_code=409,
            detail="Only an accessioned (received) specimen can be stored; "
                   "receive it at a bench first")
    if _active_storage(session, specimen.resource_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Specimen {specimen.resource_id} is already in storage; "
                   "retrieve it before storing it again")
    stored_at = _now()
    due = stored_at + timedelta(days=_hold_days(session, _specimen_type(specimen)))
    record = LabStorageRecord(
        specimen_key=specimen.resource_id, location_code=str(location_code),
        status="stored", stored_by=user.username, stored_at=stored_at,
        disposal_due=due)
    session.add(record)
    node = lab_specimens._resolve_node(session, specimen.resource_id)
    lab_specimens._append_custody(session, node, "storage", user.username,
                                  location=str(location_code),
                                  note="Stored for retention")
    node.status = "stored"
    session.flush()
    append_audit(session, actor=user.username, action="lab.preanalytical.store",
                 entity=f"Specimen/{specimen.resource_id}",
                 payload={"location_code": location_code,
                          "disposal_due": due.date().isoformat(),
                          "temperature_condition":
                          location.get("temperatureCondition")})
    session.commit()
    return {"storage": _storage_out(session, record)}


@router.get("/storage/board")
def storage_board(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("lab.read"))):
    """The storage / retention board: everything currently stored,
    everything due (or overdue) for disposal, and every excursion
    flag on a stored specimen."""
    today = _now().date()
    records = session.scalars(select(LabStorageRecord)
                              .order_by(LabStorageRecord.id.asc())).all()
    stored, due, disposed = [], [], []
    for record in records:
        out = _storage_out(session, record)
        if record.status == "stored":
            stored.append(out)
            if record.disposal_due and record.disposal_due.date() <= today:
                due.append(out)
        elif record.status == "disposed":
            disposed.append(out)
    excursions = session.scalars(select(LabStorageExcursion)
                                 .order_by(LabStorageExcursion.id.asc())).all()
    return {"stored": stored, "due_for_disposal": due, "disposed": disposed,
            "excursions": [{"id": e.id, "specimen_key": e.specimen_key,
                            "location_code": e.location_code,
                            "device_key": e.device_key,
                            "reading_id": e.reading_id, "detail": e.detail,
                            "flagged_at": e.flagged_at.isoformat()
                            if e.flagged_at else None} for e in excursions],
            "as_of": today.isoformat()}


@router.get("/storage/due")
def storage_due(session: Session = Depends(get_db),
                user: User = Depends(require_capability("lab.read"))):
    """The disposal due list: stored specimens whose governed
    retention hold has expired (due date <= today, UTC)."""
    today = _now().date()
    items = []
    for record in session.scalars(select(LabStorageRecord).where(
            LabStorageRecord.status == "stored")
            .order_by(LabStorageRecord.disposal_due.asc())).all():
        if record.disposal_due and record.disposal_due.date() <= today:
            items.append(_storage_out(session, record))
    return {"items": items, "total": len(items), "as_of": today.isoformat()}


@router.post("/storage/{specimen_key}/dispose", status_code=201)
async def dispose_specimen(specimen_key: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    """The disposal workflow's terminal step: a stored specimen is
    disposed with actor + reason, audited, and the custody chain
    gains its `disposal` event. Disposal before the retention due
    date requires an explicit override reason — the schedule is the
    rule, the override is named, never silent."""
    payload = await _json_body(request)
    reason = payload.get("reason")
    if not reason or not isinstance(reason, str):
        raise HTTPException(status_code=422,
                            detail="Disposal requires a 'reason'")
    record = _active_storage(session, specimen_key)
    if record is None:
        raise HTTPException(status_code=404,
                            detail=f"Specimen '{specimen_key}' is not in storage")
    today = _now().date()
    if record.disposal_due and record.disposal_due.date() > today \
            and not payload.get("override_reason"):
        raise HTTPException(
            status_code=409,
            detail=(f"Specimen {specimen_key} is not due for disposal until "
                    f"{record.disposal_due.date().isoformat()} under the "
                    "governed retention schedule; disposing early requires "
                    "an 'override_reason'"))
    record.status = "disposed"
    record.disposed_by = user.username
    record.disposed_at = _now()
    record.disposal_reason = reason
    node = lab_specimens._resolve_node(session, specimen_key)
    lab_specimens._append_custody(session, node, "disposal", user.username,
                                  note=f"Disposed: {reason}")
    node.status = "disposed"
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.preanalytical.dispose",
                 entity=f"Specimen/{specimen_key}",
                 payload={"reason": reason,
                          "override_reason": payload.get("override_reason"),
                          "location_code": record.location_code})
    session.commit()
    return {"storage": _storage_out(session, record)}


@router.post("/storage/{specimen_key}/retrieve", status_code=201)
async def retrieve_specimen(specimen_key: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("lab.work"))):
    """Retrieve a stored specimen — an append-only retrieval EVENT
    with a governed purpose (add-on, repeat, delta lookup, send-out,
    review). The storage placement closes as `retrieved`; storing
    the specimen again opens a new placement."""
    payload = await _json_body(request)
    purpose = payload.get("purpose")
    purposes = _config(session).get("retrievalPurposes") or list(RETRIEVAL_PURPOSES)
    if purpose not in purposes:
        raise HTTPException(
            status_code=422,
            detail=f"'purpose' must be one of {list(purposes)}")
    record = _active_storage(session, specimen_key)
    if record is None:
        raise HTTPException(status_code=404,
                            detail=f"Specimen '{specimen_key}' is not in storage")
    event = LabRetrievalEvent(specimen_key=specimen_key,
                              storage_record_id=record.id,
                              purpose=str(purpose), actor=user.username,
                              note=payload.get("note"))
    session.add(event)
    record.status = "retrieved"
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.preanalytical.retrieve",
                 entity=f"Specimen/{specimen_key}",
                 payload={"purpose": purpose,
                          "location_code": record.location_code})
    session.commit()
    return {"retrieval": {"id": event.id, "specimen_key": specimen_key,
                          "purpose": purpose, "actor": user.username,
                          "at": event.created_at.isoformat()
                          if event.created_at else None},
            "storage": _storage_out(session, record)}


@router.get("/storage/{specimen_key}/retrievals")
def specimen_retrievals(specimen_key: str, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.read"))):
    events = session.scalars(select(LabRetrievalEvent).where(
        LabRetrievalEvent.specimen_key == specimen_key)
        .order_by(LabRetrievalEvent.id.asc())).all()
    return {"items": [{"id": e.id, "purpose": e.purpose, "actor": e.actor,
                       "note": e.note,
                       "at": e.created_at.isoformat() if e.created_at else None}
                      for e in events],
            "total": len(events), "specimen_key": specimen_key}


# --- temperature-monitoring linkage (D4) ------------------------------------

FEED_HONESTY = ("Temperature linkage reads the Phase 14 device registry "
                "(app.devices): readings there are recorded/simulated "
                "inputs carrying a source label — there is NO live sensor "
                "feed in this deployment, and none is claimed. The "
                "external leg is a real sensor feed into that registry.")


@router.post("/storage/evaluate-excursions", status_code=201)
async def evaluate_excursions(request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability("lab.work"))):
    """Link stored specimens to their storage unit's temperature
    record: every excursion-flagged DeviceReading for a location's
    governed sensor, recorded while the specimen was stored there,
    flags the specimen (idempotently — one flag per reading per
    specimen)."""
    created = []
    records = session.scalars(select(LabStorageRecord).where(
        LabStorageRecord.status == "stored")).all()
    for record in records:
        location = _location(session, record.location_code)
        device_key = location.get("deviceKey")
        if not device_key:
            continue
        device = session.scalars(select(Device).where(
            Device.device_key == device_key)).first()
        if device is None:
            continue
        readings = session.scalars(select(DeviceReading).where(
            DeviceReading.device_key == device_key,
            DeviceReading.excursion.is_(True),
            DeviceReading.recorded_at >= record.stored_at)
            .order_by(DeviceReading.id.asc())).all()
        for reading in readings:
            existing = session.scalars(select(LabStorageExcursion).where(
                LabStorageExcursion.specimen_key == record.specimen_key,
                LabStorageExcursion.reading_id == reading.id)).first()
            if existing is not None:
                continue
            flag = LabStorageExcursion(
                specimen_key=record.specimen_key,
                storage_record_id=record.id,
                location_code=record.location_code,
                device_key=device_key, reading_id=reading.id,
                detail=(f"{device.name} recorded {reading.value} {reading.unit} "
                        f"({reading.metric}) at "
                        f"{reading.recorded_at.isoformat()} — excursion per "
                        "the published device-type range; reading source: "
                        f"{reading.source}"))
            session.add(flag)
            session.flush()
            created.append(flag)
            append_audit(session, actor=user.username,
                         action="lab.preanalytical.excursion_flag",
                         entity=f"Specimen/{record.specimen_key}",
                         payload={"device_key": device_key,
                                  "reading_id": reading.id,
                                  "location_code": record.location_code})
    session.commit()
    return {"flags_created": len(created),
            "flags": [{"id": f.id, "specimen_key": f.specimen_key,
                       "device_key": f.device_key, "reading_id": f.reading_id,
                       "detail": f.detail} for f in created],
            "feed": {"source": "device-registry", "live": False,
                     "note": FEED_HONESTY}}


@router.get("/storage/{specimen_key}/temperature-link")
def specimen_temperature_link(specimen_key: str,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability("lab.read"))):
    """One stored specimen's temperature linkage: its location's
    sensor, that sensor's readings during storage, and the specimen's
    excursion flags."""
    record = session.scalars(select(LabStorageRecord).where(
        LabStorageRecord.specimen_key == specimen_key)
        .order_by(LabStorageRecord.id.desc())).first()
    if record is None:
        raise HTTPException(status_code=404,
                            detail=f"Specimen '{specimen_key}' has no storage record")
    location = _location(session, record.location_code)
    device_key = location.get("deviceKey")
    readings_out, flags = [], []
    if device_key:
        readings = session.scalars(select(DeviceReading).where(
            DeviceReading.device_key == device_key,
            DeviceReading.recorded_at >= record.stored_at)
            .order_by(DeviceReading.id.asc())).all()
        readings_out = [{"id": r.id, "metric": r.metric, "value": r.value,
                         "unit": r.unit, "excursion": bool(r.excursion),
                         "source": r.source,
                         "recorded_at": r.recorded_at.isoformat()
                         if r.recorded_at else None} for r in readings]
    flags = session.scalars(select(LabStorageExcursion).where(
        LabStorageExcursion.specimen_key == specimen_key)).all()
    return {"specimen_key": specimen_key, "location_code": record.location_code,
            "device_key": device_key,
            "readings": readings_out,
            "excursion_flags": [{"id": f.id, "reading_id": f.reading_id,
                                 "detail": f.detail} for f in flags],
            "feed": {"source": "device-registry", "live": False,
                     "note": FEED_HONESTY}}


# ---------------------------------------------------------------------------
# D5 — send-out depth: catalog-as-contract packing, chase, returns
# ---------------------------------------------------------------------------

def _sendout_labs(session: Session) -> list[dict]:
    from . import lab_benches
    return lab_benches._sendout_labs(session)


def _contract_for(session: Session, entry: dict, code: str) -> dict:
    """The catalog-as-contract fields for one test at one reference
    laboratory: the published sendout-labs entry's `contracts`, with
    app.lab's seeded defaults filling any gap on an older published
    payload."""
    contracts = entry.get("contracts") or {}
    if isinstance(contracts, dict) and isinstance(contracts.get(code), dict):
        return contracts[code]
    for default_entry in lab.default_sendout_labs_payload().get("labs") or []:
        if default_entry.get("code") == entry.get("code"):
            default_contracts = default_entry.get("contracts") or {}
            if isinstance(default_contracts.get(code), dict):
                return default_contracts[code]
    return {}


def _performing_lab(session: Session, entry: dict) -> dict:
    performing = entry.get("performingLab")
    if isinstance(performing, dict):
        return performing
    for default_entry in lab.default_sendout_labs_payload().get("labs") or []:
        if default_entry.get("code") == entry.get("code") \
                and isinstance(default_entry.get("performingLab"), dict):
            return default_entry["performingLab"]
    return {"name": entry.get("display"), "code": entry.get("code")}


@router.get("/sendouts/contracts")
def sendout_contracts(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    """The send-out catalog-as-contract view: every reference
    laboratory with its performing-laboratory identity and, per
    test, the stability table, shipping temperature,
    split-before-transport instruction, necessary-information
    attachments, expected report window and FDA-cleared vs LDT
    classification."""
    out = []
    for entry in _sendout_labs(session):
        tests = []
        for code in entry.get("tests") or []:
            contract = _contract_for(session, entry, code)
            concept = lab._concept(session, code) or {}
            tests.append({
                "code": code, "display": concept.get("display"),
                "stability": contract.get("stability"),
                "shipping_temperature": contract.get("shippingTemperature"),
                "split_before_transport": contract.get("splitBeforeTransport"),
                "required_attachments": contract.get("requiredAttachments") or [],
                "report_window_hours": contract.get("reportWindowHours"),
                "classification": contract.get("classification"),
            })
        out.append({"lab_code": entry.get("code"),
                    "display": entry.get("display"),
                    "tat_hours": entry.get("tatHours"),
                    "performing_lab": _performing_lab(session, entry),
                    "tests": tests})
    return {"laboratories": out, "total": len(out)}


def _manifest_out(record: LabSendoutManifest) -> dict:
    return {"manifest_key": record.manifest_key, "lab_code": record.lab_code,
            "order_ids": record.order_ids or [],
            "shipping_temperature": record.shipping_temperature,
            "split_instructions": record.split_instructions or {},
            "attachments": record.attachments or {},
            "status": record.status, "packed_by": record.packed_by,
            "packed_at": record.packed_at.isoformat() if record.packed_at else None,
            "dispatched_at": record.dispatched_at.isoformat()
            if record.dispatched_at else None,
            "expected_report_by": record.expected_report_by.isoformat()
            if record.expected_report_by else None}


@router.post("/sendouts/pack", status_code=201)
async def pack_sendout_manifest(request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(require_capability("lab.work"))):
    """Pack a send-out manifest — the contract gate. EVERY order is
    validated BEFORE anything is written: the specimen must be
    accessioned, within its governed stability window for the
    shipping condition, packed at the contract's shipping
    temperature, and carrying every necessary-information attachment.
    Any violation refuses the WHOLE pack BY NAME and dispatches
    nothing (zero partial write)."""
    from . import lab_benches
    payload = await _json_body(request)
    order_ids = payload.get("order_ids")
    if not isinstance(order_ids, list) or not order_ids:
        raise HTTPException(status_code=422,
                            detail="'order_ids' must be a non-empty list")
    shipping_temperature = payload.get("shipping_temperature")
    if shipping_temperature not in TEMPERATURE_CONDITIONS:
        raise HTTPException(
            status_code=422,
            detail=f"'shipping_temperature' must be one of "
                   f"{list(TEMPERATURE_CONDITIONS)}")
    attachments_in = payload.get("attachments") or {}
    if not isinstance(attachments_in, dict):
        raise HTTPException(status_code=422,
                            detail="'attachments' must map order_id -> "
                                   "[attachment names]")
    now = _now()
    prepared = []
    lab_codes = set()
    for raw_id in order_ids:
        order = lab._order_row(session, str(raw_id))
        concept = lab._order_code(session, order)
        entry = lab_benches._sendout_lab_for(session, concept["code"])
        if entry is None:
            raise HTTPException(
                status_code=422,
                detail=(f"Packing refused for order {raw_id}: "
                        f"{concept.get('display')} is not a send-out test in "
                        "the published sendout-labs config"))
        if lab_benches._sendout_state(order) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Packing refused for order {raw_id}: already dispatched")
        specimen = lab._specimen_row(session, order)
        receipt = _accepted_receipt(session, specimen.resource_id) \
            if specimen is not None else None
        if specimen is None or receipt is None:
            raise HTTPException(
                status_code=409,
                detail=(f"Packing refused for order {raw_id}: the specimen "
                        "has not been accessioned at laboratory receipt — "
                        "an unaccessioned specimen cannot be packed"))
        contract = _contract_for(session, entry, concept["code"])
        stability = contract.get("stability") or {}
        hours_limit = stability.get("hours")
        collected = _parse_ts(((specimen.body or {}).get("collection") or {})
                              .get("collectedDateTime"))
        if isinstance(hours_limit, (int, float)) and collected is not None:
            age_hours = (now - collected).total_seconds() / 3600.0
            if age_hours > float(hours_limit):
                raise HTTPException(
                    status_code=422,
                    detail=(f"Packing refused for order {raw_id}: specimen "
                            f"stability expired — collected "
                            f"{collected.isoformat()}, {round(age_hours, 1)} h "
                            f"ago, beyond the contract stability limit of "
                            f"{hours_limit} h at "
                            f"{stability.get('condition') or 'the shipped condition'} "
                            f"for {concept['code']} at {entry.get('display')}"))
        required_temp = contract.get("shippingTemperature")
        if required_temp and shipping_temperature != required_temp:
            raise HTTPException(
                status_code=422,
                detail=(f"Packing refused for order {raw_id}: wrong shipping "
                        f"temperature — the contract for {concept['code']} at "
                        f"{entry.get('display')} requires '{required_temp}', "
                        f"the pack specified '{shipping_temperature}'"))
        required_attachments = contract.get("requiredAttachments") or []
        provided = attachments_in.get(str(raw_id)) or []
        missing = [a for a in required_attachments if a not in provided]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(f"Packing refused for order {raw_id}: missing "
                        f"necessary-information attachment(s) {missing} "
                        f"required by the {entry.get('display')} contract "
                        f"for {concept['code']} — dispatch is gated until "
                        "they are attached"))
        lab_codes.add(entry.get("code"))
        prepared.append((order, entry, concept, contract))
    if len(lab_codes) > 1:
        raise HTTPException(
            status_code=422,
            detail=("A send-out manifest ships to ONE reference laboratory; "
                    "split the batch by laboratory"))
    entry = prepared[0][1]
    manifest = LabSendoutManifest(
        manifest_key=str(uuid.uuid4()), lab_code=entry.get("code"),
        order_ids=[o.resource_id for o, _e, _c, _k in prepared],
        shipping_temperature=str(shipping_temperature),
        split_instructions={c["code"]: k.get("splitBeforeTransport")
                            for _o, _e, c, k in prepared},
        attachments={o.resource_id: list(attachments_in.get(o.resource_id) or [])
                     for o, _e, _c, _k in prepared},
        status="packed", packed_by=user.username, packed_at=now)
    session.add(manifest)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.preanalytical.sendout_pack",
                 entity=f"LabSendoutManifest/{manifest.manifest_key}",
                 payload={"lab": entry.get("code"),
                          "orders": manifest.order_ids,
                          "shipping_temperature": shipping_temperature})
    session.commit()
    return {"manifest": _manifest_out(manifest)}


@router.post("/sendouts/manifests/{manifest_key}/dispatch", status_code=201)
async def dispatch_packed_manifest(manifest_key: str, request: Request,
                                   session: Session = Depends(get_db),
                                   user: User = Depends(require_capability("lab.work"))):
    """Dispatch a PACKED manifest. Only a packed manifest can ship —
    the packing gate has already enforced the contract — and dispatch
    writes the identical send-out state the existing machinery uses
    (app.lab_benches helpers), stamping the expected report-by
    instant from the contract's report window."""
    from . import lab_benches
    manifest = session.scalars(select(LabSendoutManifest).where(
        LabSendoutManifest.manifest_key == manifest_key)).first()
    if manifest is None:
        raise HTTPException(status_code=404,
                            detail=f"Manifest '{manifest_key}' not found")
    if manifest.status != "packed":
        raise HTTPException(
            status_code=409,
            detail=f"Manifest {manifest_key} is '{manifest.status}'; only a "
                   "packed manifest can be dispatched (packing enforces "
                   "the reference-laboratory contract first)")
    payload = await _json_body(request)
    now = _now()
    windows = []
    for order_id in manifest.order_ids or []:
        order = lab._order_row(session, order_id)
        concept = lab._order_code(session, order)
        entry = lab_benches._sendout_lab_for(session, concept["code"])
        contract = _contract_for(session, entry, concept["code"]) if entry else {}
        window = contract.get("reportWindowHours")
        if not isinstance(window, (int, float)) or isinstance(window, bool):
            window = (entry or {}).get("tatHours")
        if isinstance(window, (int, float)) and not isinstance(window, bool):
            windows.append(float(window))
        lab_benches._set_sendout_state(
            order, status="dispatched", lab=manifest.lab_code,
            manifest_id=manifest.manifest_key, dispatched_at=_now_iso(),
            tracking_ref=payload.get("tracking_ref"),
            performing_lab=_performing_lab(session, entry) if entry else None,
            classification=contract.get("classification"))
        lab._set_status(order, "in-process")
        specimen = lab._specimen_row(session, order)
        if specimen is not None:
            lab._set_status(specimen, "in-process")
        append_audit(session, actor=user.username,
                     action="lab.sendout.dispatch",
                     entity=f"ServiceRequest/{order_id}",
                     payload={"manifest_id": manifest.manifest_key,
                              "lab": manifest.lab_code,
                              "via": "preanalytical-packed-manifest"})
    manifest.status = "dispatched"
    manifest.dispatched_at = now
    if windows:
        manifest.expected_report_by = now + timedelta(hours=max(windows))
    session.flush()
    session.commit()
    return {"manifest": _manifest_out(manifest)}


@router.get("/sendouts/manifests")
def list_manifests(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(select(LabSendoutManifest)
                           .order_by(LabSendoutManifest.id.asc())).all()
    return {"items": [_manifest_out(r) for r in rows], "total": len(rows)}


@router.get("/sendouts/chase-list")
def sendout_chase_list(session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.read"))):
    """The overdue chase list: dispatched send-outs whose expected
    report window (the contract's reportWindowHours, falling back to
    the laboratory's contracted tatHours) has elapsed with no report
    received."""
    from . import lab_benches
    now = _now()
    items = []
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "ServiceRequest")).all()
    for row in rows:
        state = lab_benches._sendout_state(row)
        if not state or state.get("status") != "dispatched":
            continue
        dispatched = _parse_ts(state.get("dispatched_at"))
        if dispatched is None:
            continue
        code = lab._code_of(row.body or {})
        entry = lab_benches._sendout_lab_for(session, code) if code else None
        contract = _contract_for(session, entry, code) if entry else {}
        window = contract.get("reportWindowHours")
        if not isinstance(window, (int, float)) or isinstance(window, bool):
            window = (entry or {}).get("tatHours")
        if not isinstance(window, (int, float)) or isinstance(window, bool):
            continue
        due = dispatched + timedelta(hours=float(window))
        if now > due:
            items.append({
                "order_id": row.resource_id, "code": code,
                "lab_code": state.get("lab"),
                "manifest_id": state.get("manifest_id"),
                "dispatched_at": state.get("dispatched_at"),
                "expected_by": due.isoformat(),
                "overdue_hours": round((now - due).total_seconds() / 3600.0, 2),
                "report_window_hours": float(window),
                "performing_lab": state.get("performing_lab"),
                "classification": state.get("classification"),
            })
    items.sort(key=lambda i: -i["overdue_hours"])
    return {"items": items, "total": len(items), "as_of": _now_iso()}


@router.post("/sendouts/{order_id}/receive-result", status_code=201)
async def receive_sendout_result(order_id: str, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability("lab.work"))):
    """Receive a send-out report through the EXISTING receive-result
    machinery (app.lab_benches), then stamp the PERFORMING
    LABORATORY's identity + the test's FDA-cleared vs LDT
    classification onto the received DiagnosticReport — the report
    shows who actually performed the test."""
    from . import lab_benches
    payload = await _json_body(request)
    order = lab._order_row(session, order_id)
    concept = lab._order_code(session, order)
    entry = lab_benches._sendout_lab_for(session, concept["code"])
    if entry is None:
        raise HTTPException(
            status_code=422,
            detail=(f"Order {order_id} ({concept.get('display')}) is not "
                    "a send-out test in the published sendout-labs config"))
    outcome = await lab_benches.sendout_receive_result(
        order_id, _ReplayRequest(payload), session, user)
    performing = _performing_lab(session, entry)
    contract = _contract_for(session, entry, concept["code"])
    report_id = outcome["report"]["id"]
    report_row = fhir._get_row("DiagnosticReport", report_id, session)
    body = dict(report_row.body or {})
    body["performer"] = [{"display": performing.get("name"),
                          "reference": None,
                          "identifier": {"system": "urn:opencare:reference-lab",
                                         "value": performing.get("code")
                                         or entry.get("code")}}]
    exts = [e for e in body.get("extension") or []
            if not (isinstance(e, dict) and e.get("url") == PERFORMING_LAB_EXTENSION)]
    exts.append({"url": PERFORMING_LAB_EXTENSION,
                 "valueString": (f"{performing.get('name')} "
                                 f"({performing.get('code') or entry.get('code')}); "
                                 f"classification: "
                                 f"{contract.get('classification') or 'unspecified'}")})
    body["extension"] = exts
    report_row.body = body
    report_row.version += 1
    lab_benches._set_sendout_state(
        order, performing_lab=performing,
        classification=contract.get("classification"),
        report_id=report_id)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.preanalytical.sendout_result_stamped",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"performing_lab": performing,
                          "classification": contract.get("classification")})
    session.commit()
    return {**outcome, "performing_lab": performing,
            "classification": contract.get("classification"),
            "sendout": lab_benches._sendout_state(order)}


@router.post("/sendouts/{order_id}/return", status_code=201)
async def return_sendout_specimen(order_id: str, request: Request,
                                  session: Session = Depends(get_db),
                                  user: User = Depends(require_capability("lab.work"))):
    """Send-out rejection / unsuitable return: the reference
    laboratory returned the specimen with a CODED reason. The
    send-out state is closed into the return history, the order and
    specimen return to `collected`, the accepted receipt is retired,
    and the specimen RE-ENTERS the receiving queue carrying its
    return reason."""
    from . import lab_benches
    payload = await _json_body(request)
    reason_code = payload.get("reason_code")
    reasons = {r.get("code"): r
               for r in _config(session).get("sendoutReturnReasons") or []
               if isinstance(r, dict)}
    if reason_code not in reasons:
        raise HTTPException(
            status_code=422,
            detail=(f"'reason_code' must be one of the governed send-out "
                    f"return reasons {sorted(reasons)}"))
    order = lab._order_row(session, order_id)
    state = lab_benches._sendout_state(order)
    if state is None or state.get("status") != "dispatched":
        raise HTTPException(
            status_code=409,
            detail=(f"Order {order_id} is not awaiting a send-out result "
                    f"(send-out status: {(state or {}).get('status', 'not dispatched')}); "
                    "only a dispatched specimen can be returned"))
    specimen = lab._specimen_row(session, order)
    record = LabSendoutReturn(
        order_id=order_id,
        specimen_key=specimen.resource_id if specimen else None,
        lab_code=state.get("lab"), reason_code=str(reason_code),
        reason_display=reasons[reason_code].get("display"),
        note=payload.get("note"), recorded_by=user.username)
    session.add(record)
    body = dict(order.body or {})
    history = list(body.get("sendoutReturns") or [])
    history.append({"reason_code": reason_code,
                    "reason_display": reasons[reason_code].get("display"),
                    "lab": state.get("lab"),
                    "manifest_id": state.get("manifest_id"),
                    "returned_at": _now_iso(), "by": user.username})
    body["sendoutReturns"] = history
    body.pop("sendout", None)
    order.body = body
    lab._set_status(order, "collected")
    if specimen is not None:
        sbody = dict(specimen.body or {})
        sbody.pop("receivedDateTime", None)
        specimen.body = sbody
        lab._set_status(specimen, "collected")
        receipt = _accepted_receipt(session, specimen.resource_id)
        if receipt is not None:
            receipt.status = "returned"
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.preanalytical.sendout_return",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"reason_code": reason_code,
                          "lab": state.get("lab"),
                          "manifest_id": state.get("manifest_id")})
    session.commit()
    return {"return": {"id": record.id, "order_id": order_id,
                       "specimen_key": record.specimen_key,
                       "reason_code": reason_code,
                       "reason_display": reasons[reason_code].get("display")},
            "order_status": "collected",
            "re_enters_receipt": True}
