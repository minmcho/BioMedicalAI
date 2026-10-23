"""Phase 15 (C/H-1) — barcode registry, universal lookup + label printing.

What lives here:

- **Barcode standards are published Studio config**
  (`integration/barcode-standards`): the kind vocabulary
  (wristband | specimen | aliquot | blood | medication | asset), the
  per-kind prefix and the payload separator. This EXTENDS the Phase 11
  BCMA pattern (app.pharmacy_ops `barcodeStandards` in
  terminology/pharmacy-ops: wristband OCWB1, med label OCML1,
  separator "|") to every labelled thing in the hospital. ISBT 128
  governs the blood kind: its values are ISBT 128 Donation
  Identification Numbers (data identifier "=" + facility id + year +
  serial), validated by the published pattern, not by a local prefix.

- **Registry** (`BarcodeRecord`, this module's own table): every
  issued barcode value, unique, bound to an entity_ref
  ("Patient/<id>", "Specimen/<id>", "BloodUnit/<unit_id>",
  "MedicationDispense/<id>", "Device/<device_key>", ...), with its
  label payload fields and a status (active | revoked). Registration
  validates the value against the kind's published prefix/format —
  a value that does not match its kind's standard is a 422, never a
  silently accepted mislabel. Every registration / revocation /
  label print is hash-audit-chained and also lands in the record's
  own event history (`BarcodeEvent`).

- **Universal lookup** (`GET /barcodes/lookup?value=`): one scan
  resolves ANY registered barcode to what it is — kind, the bound
  entity's summary, and its history. Specimen/aliquot lookups also
  carry the family chain summary, read DEFENSIVELY from the lab
  workstream's `SpecimenBarcode` table (app.lab_specimens, built in
  parallel — imported lazily with getattr fallbacks, so this module
  works whether or not that table has landed, and never breaks if
  its column names differ). Medication lookups carry the BCMA /
  dispense context when the dispense resolves. Wristband lookups
  return patient identity — staff-only, enforced by the
  `barcode.read` capability gate (the patient role does not hold it).
  An unknown value is a 404 whose payload says plainly that the
  value is not a registered barcode.

- **Label printing** (`GET /barcodes/labels/{kind}/{entity_ref}`):
  a print-ready label PDF. app.reports.build_pdf renders full-page
  letterhead documents (invoices, statements) — that shape cannot
  honestly BE a physical label, so labels are rendered directly
  with ReportLab on label-sized pages using the published print
  template's org identity values (integration/print: org name,
  address, confidentiality notice). Every render writes the
  `reports.document.print` audit row — the Phase 13 print
  confidentiality precedent (who printed what, when).

- **Seeded demo barcodes**: ensure defaults registers demo barcodes
  (wristband for the first demo patient, one specimen, one blood
  unit, one medication) ONLY when the referenced entities already
  exist — the guard keeps unit tests (fresh database, no clinical
  data at startup) clean, and re-runs idempotent.
"""
import io
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import (DateTime, Integer, JSON, String, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import (BloodUnit, ConfigStatus, Device, FhirResource,
                     PharmacyLot, StudioConfig, User, utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/barcodes", tags=["barcodes"])

STANDARDS_KEY = "barcode-standards"   # integration/barcode-standards
PRINT_KEY = "print"                   # integration/print
KINDS = ("wristband", "specimen", "aliquot", "blood", "medication", "asset")
STATUSES = ("active", "revoked")


# ---------------------------------------------------------------------------
# This module's own tables (the protocols.py / report_engine.py pattern)
# ---------------------------------------------------------------------------

class BarcodeRecord(Base):
    """One registered barcode: a unique value, its kind, the entity it
    is bound to, the label payload fields printed for it, and a status.
    The registry is what makes a scan resolvable — a value that is not
    in this table is not an OpenCare barcode, whatever it looks like."""
    __tablename__ = "barcode_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[str] = mapped_column(String(200), unique=True,
                                       nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    entity_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                            index=True)
    label_fields: Mapped[dict] = mapped_column(JSON, nullable=False,
                                               default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="active", index=True)
    issued_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                           default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


class BarcodeEvent(Base):
    """One entry in a barcode's own history (registered, label printed,
    revoked) — the per-barcode chain the universal lookup returns.
    Mirrors the hash-chained audit rows written alongside it."""
    __tablename__ = "barcode_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    barcode_value: Mapped[str] = mapped_column(String(200), nullable=False,
                                               index=True)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Published barcode standards (integration/barcode-standards)
# ---------------------------------------------------------------------------

def default_standards_payload() -> dict:
    """Synthetic barcode-standards defaults. Per-kind prefix +
    separator, extending the Phase 11 pharmacy-ops barcodeStandards
    (wristband OCWB1 / med label OCML1 / separator "|") to specimens,
    aliquots and assets. Blood is ISBT 128: the value is a Donation
    Identification Number — data identifier "=", 5-char facility id,
    2-digit year, 6-digit serial — validated by pattern, and flagged
    with its governing standard so no site mistakes it for a local
    prefix scheme."""
    return {
        "name": "Barcode standards (synthetic defaults)",
        "separator": "|",
        "kinds": {
            "wristband": {"prefix": "OCWB1",
                          "standard": "OpenCare wristband payload "
                                      "(Phase 11 BCMA pattern)"},
            "specimen": {"prefix": "OCSP1",
                         "standard": "OpenCare specimen label"},
            "aliquot": {"prefix": "OCAL1",
                        "standard": "OpenCare aliquot label "
                                    "(child of a specimen barcode)"},
            "blood": {"prefix": "=",
                      "standard": "ISBT 128",
                      "pattern": r"^=[A-Z0-9]{5}[0-9]{8}$",
                      "patternDescription": ("ISBT 128 Donation "
                                             "Identification Number: '=' "
                                             "+ 5-char facility id + "
                                             "2-digit year + 6-digit "
                                             "serial")},
            "medication": {"prefix": "OCML1",
                           "standard": "OpenCare unit-dose med label "
                                       "(Phase 11 BCMA pattern)"},
            "asset": {"prefix": "OCAS1",
                      "standard": "OpenCare asset tag"},
        },
    }


def _standards(session: Session) -> dict:
    cfg = published_config(session, "integration", STANDARDS_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    if isinstance(payload.get("kinds"), dict) and payload["kinds"]:
        return payload
    return default_standards_payload()


def _kind_cfg(standards: dict, kind: str) -> dict | None:
    entry = (standards.get("kinds") or {}).get(kind)
    return entry if isinstance(entry, dict) else None


def validate_value(standards: dict, kind: str, value: str) -> str | None:
    """None when `value` conforms to the kind's published standard,
    else the reason it does not (the registration 422 detail)."""
    cfg = _kind_cfg(standards, kind)
    if cfg is None:
        return (f"kind '{kind}' has no entry in the published "
                "barcode standards")
    pattern = cfg.get("pattern")
    if pattern:
        if not re.match(str(pattern), value):
            return (f"value '{value}' does not match the {kind} "
                    f"standard ({cfg.get('standard') or 'published'}): "
                    f"{cfg.get('patternDescription') or pattern}")
        return None
    prefix = str(cfg.get("prefix") or "")
    separator = str(standards.get("separator") or "|")
    if not value.startswith(prefix):
        return (f"value '{value}' does not start with the published "
                f"{kind} prefix '{prefix}'")
    rest = value[len(prefix):]
    if not rest.startswith(separator) or len(rest) < 2:
        return (f"value '{value}' must be "
                f"'{prefix}{separator}<payload>' per the published "
                f"{kind} standard")
    return None


def _record_out(row: BarcodeRecord) -> dict:
    return {"value": row.value, "kind": row.kind,
            "entity_ref": row.entity_ref,
            "label_fields": row.label_fields or {},
            "status": row.status, "issued_by": row.issued_by,
            "created_at": row.created_at.isoformat()
            if row.created_at else None}


def _history(session: Session, value: str) -> list[dict]:
    rows = session.scalars(
        select(BarcodeEvent).where(BarcodeEvent.barcode_value == value)
        .order_by(BarcodeEvent.id.asc())).all()
    return [{"event": r.event, "actor": r.actor, "detail": r.detail or {},
             "at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]


def _add_event(session: Session, value: str, event: str, actor: str,
               detail: dict) -> None:
    session.add(BarcodeEvent(barcode_value=value, event=event, actor=actor,
                             detail=detail))
    session.flush()


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


def _require_str(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"'{field}' is required")
    return value.strip()


# ---------------------------------------------------------------------------
# Entity resolution — what a bound entity_ref actually is
# ---------------------------------------------------------------------------

def _fhir_row(session: Session, resource_type: str,
              resource_id: str) -> FhirResource | None:
    return session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == resource_type,
            FhirResource.resource_id == resource_id)).first()


def _split_ref(entity_ref: str) -> tuple[str, str]:
    if "/" in entity_ref:
        head, tail = entity_ref.split("/", 1)
        return head, tail
    return "", entity_ref


def _patient_summary(session: Session, patient_id: str) -> dict | None:
    row = _fhir_row(session, "Patient", patient_id)
    if row is None:
        return None
    body = row.body or {}
    name = (body.get("name") or [{}])[0]
    display = name.get("text") or " ".join(
        filter(None, [" ".join(name.get("given") or []),
                      name.get("family") or ""])).strip()
    mrn = None
    for ident in body.get("identifier") or []:
        if isinstance(ident, dict) and ident.get("value"):
            mrn = str(ident["value"])
            break
    return {"resource": f"Patient/{patient_id}", "name": display or None,
            "mrn": mrn or patient_id, "birth_date": body.get("birthDate"),
            "gender": body.get("gender")}


def _specimen_family(session: Session, record: BarcodeRecord,
                     specimen_id: str) -> dict:
    """Family chain summary for a specimen/aliquot barcode: the
    specimen record itself plus whatever the lab workstream's
    SpecimenBarcode table knows (parent/children/custody neighbours).
    DEFENSIVE by design: app.lab_specimens is a parallel Phase 15
    build — imported lazily, columns read via getattr fallbacks, and
    any failure degrades to the FHIR specimen summary alone."""
    summary: dict = {"specimen_id": specimen_id,
                     "family_barcodes": [], "source": "fhir-specimen"}
    row = _fhir_row(session, "Specimen", specimen_id)
    if row is not None:
        body = row.body or {}
        summary["accession"] = (body.get("accessionIdentifier") or {}).get("value")
        summary["status"] = body.get("status")
        summary["type"] = (body.get("type") or {}).get("text")
        parents = body.get("parent") or []
        summary["parent_refs"] = [
            p.get("reference") for p in parents if isinstance(p, dict)]
    try:
        from . import lab_specimens
        model = getattr(lab_specimens, "SpecimenBarcode", None)
        if model is None:
            return summary
        columns = set(model.__table__.columns.keys())
        rows = session.scalars(select(model)).all()

        def cell(r, *names):
            for n in names:
                if n in columns:
                    v = getattr(r, n, None)
                    if v is not None:
                        return v
            return None

        mine = [r for r in rows
                if cell(r, "value", "barcode", "barcode_value")
                == record.value]
        related = []
        my_refs = {cell(r, "entity_ref", "specimen_ref", "entity_id",
                        "specimen_id") for r in mine}
        for r in rows:
            ref = cell(r, "entity_ref", "specimen_ref", "entity_id",
                       "specimen_id")
            parent = cell(r, "parent_value", "parent_barcode",
                          "parent_ref")
            if r in mine:
                continue
            if (ref is not None and ref in my_refs) \
                    or parent == record.value \
                    or (mine and parent is not None
                        and parent == cell(mine[0], "parent_value",
                                           "parent_barcode", "parent_ref")):
                related.append({
                    "value": cell(r, "value", "barcode", "barcode_value"),
                    "kind": cell(r, "kind", "barcode_kind"),
                    "entity_ref": ref, "parent_value": parent})
        summary["family_barcodes"] = related
        summary["source"] = "lab_specimens.SpecimenBarcode"
    except Exception:
        # The parallel table has not landed (or differs): the FHIR
        # specimen summary above is the honest answer available.
        pass
    return summary


def _entity_summary(session: Session, record: BarcodeRecord) -> dict:
    """Resolve the bound entity to a summary, per kind. Unresolvable
    entities are reported as such — never invented."""
    ref_type, ref_id = _split_ref(record.entity_ref)
    kind = record.kind
    if kind == "wristband" or ref_type == "Patient":
        patient = _patient_summary(session, ref_id)
        if patient is None:
            return {"resolved": False, "entity_ref": record.entity_ref,
                    "reason": f"Patient/{ref_id} not found"}
        return {"resolved": True, "patient": patient}
    if kind in ("specimen", "aliquot"):
        family = _specimen_family(session, record, ref_id)
        return {"resolved": True, "specimen": family,
                "family_chain": family}
    if kind == "blood" or ref_type == "BloodUnit":
        unit = session.scalars(select(BloodUnit).where(
            BloodUnit.unit_id == ref_id)).first()
        if unit is None:
            return {"resolved": False, "entity_ref": record.entity_ref,
                    "reason": f"BloodUnit/{ref_id} not found"}
        return {"resolved": True, "blood_unit": {
            "unit_id": unit.unit_id, "donation_id": unit.unit_id,
            "product_code": unit.product_type,
            "product_type": unit.product_type,
            "blood_group": unit.blood_group,
            "expiry_date": unit.expiry_date, "status": unit.status,
                "patient_ref": unit.patient_ref,
            "order_id": unit.order_id}}
    if kind == "medication" or ref_type == "MedicationDispense":
        row = _fhir_row(session, "MedicationDispense", ref_id)
        if row is None:
            lot = session.scalars(select(PharmacyLot).where(
                PharmacyLot.lot_number == ref_id)).first()
            if lot is not None:
                return {"resolved": True, "pharmacy_lot": {
                    "medication_code": lot.medication_code,
                    "location": lot.location,
                    "lot_number": lot.lot_number,
                    "expiry_date": lot.expiry_date,
                    "quantity": lot.quantity},
                    "bcma": {"context": "lot",
                             "note": "Bound to a pharmacy lot, not a "
                                     "unit-dose dispense"}}
            return {"resolved": False, "entity_ref": record.entity_ref,
                    "reason": f"MedicationDispense/{ref_id} not found"}
        body = row.body or {}
        from . import orders as orders_mod
        _, code, display = orders_mod._code_of(
            body, "medicationCodeableConcept")
        lot_number = None
        for ext in body.get("extension") or []:
            if isinstance(ext, dict) and "lot" in str(ext.get("url") or ""):
                lot_number = ext.get("valueString")
        prescriptions = body.get("authorizingPrescription") or []
        return {"resolved": True, "dispense": {
            "dispense_id": ref_id, "status": body.get("status"),
            "medication_code": code, "medication_display": display,
            "lot_number": lot_number,
            "patient_ref": row.patient_ref
            or (body.get("subject") or {}).get("reference"),
            "when_handed_over": body.get("whenHandedOver")},
            "bcma": {"context": "dispense",
                     "order_ref": (prescriptions[0] or {}).get("reference")
                     if prescriptions else None,
                     "note": "Scan context for the BCMA five-rights "
                             "check (app.pharmacy_ops)"}}
    if kind == "asset" or ref_type == "Device":
        device = session.scalars(select(Device).where(
            Device.device_key == ref_id)).first()
        if device is None:
            return {"resolved": False, "entity_ref": record.entity_ref,
                    "reason": f"Device/{ref_id} not found"}
        return {"resolved": True, "device": {
            "device_key": device.device_key, "name": device.name,
            "device_type": device.device_type,
            "location": device.location, "serial": device.serial,
            "status": device.status}}
    return {"resolved": False, "entity_ref": record.entity_ref,
            "reason": f"No resolver for entity reference "
                      f"'{record.entity_ref}'"}


# ---------------------------------------------------------------------------
# Registry API
# ---------------------------------------------------------------------------

@router.get("/standards")
def get_standards(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("barcode.read"))):
    """The published barcode standards the registry validates
    against (integration/barcode-standards)."""
    return {"standards": _standards(session), "kinds": list(KINDS)}


@router.get("")
def list_barcodes(kind: str | None = Query(default=None),
                  status: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability("barcode.read"))):
    stmt = select(BarcodeRecord).order_by(BarcodeRecord.id.asc())
    if kind:
        stmt = stmt.where(BarcodeRecord.kind == kind)
    if status:
        stmt = stmt.where(BarcodeRecord.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_record_out(r) for r in rows], "total": len(rows)}


@router.post("", status_code=201)
async def register_barcode(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(
                               require_capability("barcode.manage"))):
    """Register one barcode value against its entity. The value is
    validated against the kind's published standard (422 on
    mismatch); a value may be registered exactly once (409)."""
    payload = await _json_body(request)
    value = _require_str(payload, "value")
    kind = _require_str(payload, "kind")
    entity_ref = _require_str(payload, "entity_ref")
    if kind not in KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"'kind' must be one of {list(KINDS)}")
    standards = _standards(session)
    problem = validate_value(standards, kind, value)
    if problem is not None:
        raise HTTPException(status_code=422, detail=problem)
    if session.scalars(select(BarcodeRecord).where(
            BarcodeRecord.value == value)).first() is not None:
        raise HTTPException(status_code=409,
                            detail=f"Barcode '{value}' is already "
                                   "registered")
    label_fields = payload.get("label_fields")
    if label_fields is not None and not isinstance(label_fields, dict):
        raise HTTPException(status_code=422,
                            detail="'label_fields' must be an object")
    record = BarcodeRecord(value=value, kind=kind, entity_ref=entity_ref,
                           label_fields=label_fields or {},
                           status="active", issued_by=user.username)
    session.add(record)
    session.flush()
    _add_event(session, value, "registered", user.username,
               {"kind": kind, "entity_ref": entity_ref})
    append_audit(session, actor=user.username, action="barcodes.register",
                 entity=f"Barcode/{value}",
                 payload={"kind": kind, "entity_ref": entity_ref,
                          "standard": (_kind_cfg(standards, kind) or {})
                          .get("standard")})
    session.commit()
    return _record_out(record)


@router.post("/{value:path}/revoke")
def revoke_barcode(value: str, session: Session = Depends(get_db),
                   user: User = Depends(
                       require_capability("barcode.manage"))):
    """Retire a barcode value (damaged / misprinted label): the value
    stays resolvable in lookup — with status revoked — so a scan of a
    withdrawn label is explained, not mysterious."""
    record = session.scalars(select(BarcodeRecord).where(
        BarcodeRecord.value == value)).first()
    if record is None:
        raise HTTPException(status_code=404,
                            detail=f"Barcode '{value}' is not registered")
    if record.status == "revoked":
        raise HTTPException(status_code=409,
                            detail=f"Barcode '{value}' is already revoked")
    record.status = "revoked"
    session.flush()
    _add_event(session, value, "revoked", user.username, {})
    append_audit(session, actor=user.username, action="barcodes.revoke",
                 entity=f"Barcode/{value}",
                 payload={"kind": record.kind,
                          "entity_ref": record.entity_ref})
    session.commit()
    return _record_out(record)


@router.get("/lookup")
def lookup_barcode(value: str = Query(...),
                   session: Session = Depends(get_db),
                   user: User = Depends(
                       require_capability("barcode.read"))):
    """THE universal scan: any registered barcode value resolves to
    its kind, its bound entity's summary and its history. Unknown
    values get an honest 404 — 'not a registered barcode' — never a
    guessed identity."""
    record = session.scalars(select(BarcodeRecord).where(
        BarcodeRecord.value == value)).first()
    if record is None:
        return JSONResponse(status_code=404, content={
            "detail": "not a registered barcode",
            "registered": False, "value": value,
            "message": (f"'{value}' is not a registered barcode in this "
                        "system. It may be an external manufacturer's "
                        "code, a damaged label, or a label from another "
                        "facility — do not act on it as an OpenCare "
                        "identifier.")})
    return {"registered": True, "value": record.value, "kind": record.kind,
            "entity_ref": record.entity_ref, "status": record.status,
            "issued_by": record.issued_by,
            "label_fields": record.label_fields or {},
            "entity": _entity_summary(session, record),
            "history": _history(session, record.value)}


# ---------------------------------------------------------------------------
# Label printing — ReportLab on label-sized pages, org identity from the
# published print template (integration/print). build_pdf renders
# full-page letterhead documents; a label is rendered as a label.
# ---------------------------------------------------------------------------

def _print_template(session: Session) -> dict:
    cfg = published_config(session, "integration", PRINT_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    documents = payload.get("documents")
    if isinstance(documents, dict) and documents.get("orgName"):
        return documents
    from .reports import default_print_payload
    return default_print_payload()["documents"]


# Label page sizes (points): a wristband strip, and 4x2" stock labels.
_LABEL_SIZES = {"wristband": (288, 72), "specimen": (288, 144),
                "aliquot": (288, 144), "blood": (288, 144),
                "medication": (288, 144), "asset": (288, 144)}


def _label_pdf(template: dict, kind: str, title: str,
               lines: list[str], barcode_value: str) -> bytes:
    from reportlab.pdfgen import canvas as pdf_canvas

    width, height = _LABEL_SIZES.get(kind, (288, 144))
    buffer = io.BytesIO()
    c = pdf_canvas.Canvas(buffer, pagesize=(width, height))
    c.setTitle(f"{kind} label")
    y = height - 14
    c.setFont("Helvetica-Bold", 7)
    c.drawString(8, y, str(template.get("orgName") or "OpenCare")[:60])
    y -= 11
    c.setFont("Helvetica-Bold", 9)
    c.drawString(8, y, title[:52])
    y -= 11
    c.setFont("Helvetica", 8)
    for line in lines:
        if y < 26:
            break
        c.drawString(8, y, str(line)[:58])
        y -= 10
    c.setFont("Courier-Bold", 10)
    c.drawString(8, 14, barcode_value[:44])
    notice = str(template.get("confidentialityNotice") or "")
    if notice and kind == "wristband":
        c.setFont("Helvetica-Oblique", 5.5)
        c.drawString(8, 5, notice[:90])
    c.showPage()
    c.save()
    return buffer.getvalue()


def _label_content(session: Session, kind: str,
                   entity_ref: str) -> tuple[str, list[str], str]:
    """(title, field lines, barcode value) for one label, or raise
    404 when the entity does not exist. The barcode value is the
    REGISTERED value for the entity when one exists, else the value
    the kind's standard generates for it."""
    ref_type, ref_id = _split_ref(entity_ref)
    standards = _standards(session)
    separator = str(standards.get("separator") or "|")
    registered = session.scalars(
        select(BarcodeRecord).where(BarcodeRecord.kind == kind,
                                    BarcodeRecord.entity_ref
                                    == entity_ref)).first()
    if registered is None and ref_type:
        registered = session.scalars(
            select(BarcodeRecord).where(
                BarcodeRecord.kind == kind,
                BarcodeRecord.entity_ref == ref_id)).first()

    def value_or(default: str) -> str:
        return registered.value if registered is not None else default

    if kind == "wristband":
        patient = _patient_summary(session, ref_id)
        if patient is None:
            raise HTTPException(status_code=404,
                                detail=f"Patient/{ref_id} not found")
        prefix = (_kind_cfg(standards, "wristband") or {}).get("prefix",
                                                               "OCWB1")
        dob_digits = str(patient["birth_date"] or "").replace("-", "")
        default = f"{prefix}{separator}{ref_id}{separator}{dob_digits}"
        lines = [f"Name: {patient['name'] or ''}",
                 f"MRN: {patient['mrn']}",
                 f"DOB: {patient['birth_date'] or ''}"]
        return "Patient wristband", lines, value_or(default)
    if kind in ("specimen", "aliquot"):
        row = _fhir_row(session, "Specimen", ref_id)
        if row is None:
            raise HTTPException(status_code=404,
                                detail=f"Specimen/{ref_id} not found")
        body = row.body or {}
        accession = (body.get("accessionIdentifier") or {}).get("value")
        parents = [p.get("reference") for p in body.get("parent") or []
                   if isinstance(p, dict)]
        prefix = (_kind_cfg(standards, kind) or {}).get(
            "prefix", "OCSP1" if kind == "specimen" else "OCAL1")
        lines = [f"Accession: {accession or ''}",
                 f"Type: {(body.get('type') or {}).get('text') or ''}",
                 f"Status: {body.get('status') or ''}"]
        if parents:
            lines.append(f"Parent: {parents[0]}")
        return f"{kind.capitalize()} label", lines, value_or(
            f"{prefix}{separator}{ref_id}")
    if kind == "blood":
        unit = session.scalars(select(BloodUnit).where(
            BloodUnit.unit_id == ref_id)).first()
        if unit is None:
            raise HTTPException(status_code=404,
                                detail=f"BloodUnit/{ref_id} not found")
        lines = [f"DIN (ISBT 128): {unit.unit_id}",
                 f"Product: {unit.product_type}",
                 f"Blood group: {unit.blood_group}",
                 f"Expiry: {unit.expiry_date}"]
        return "Blood unit label (ISBT 128)", lines, value_or(unit.unit_id)
    if kind == "medication":
        row = _fhir_row(session, "MedicationDispense", ref_id)
        if row is not None:
            from . import orders as orders_mod
            _, code, display = orders_mod._code_of(
                row.body or {}, "medicationCodeableConcept")
            lot_number = None
            for ext in (row.body or {}).get("extension") or []:
                if isinstance(ext, dict) \
                        and "lot" in str(ext.get("url") or ""):
                    lot_number = ext.get("valueString")
            prefix = (_kind_cfg(standards, "medication") or {}).get(
                "prefix", "OCML1")
            lines = [f"Medication: {display or code or ''}",
                     f"Code: {code or ''}",
                     f"Lot: {lot_number or '-'}",
                     f"Dispense: {ref_id}"]
            return "Medication label", lines, value_or(
                f"{prefix}{separator}{ref_id}{separator}"
                f"{lot_number or '-'}")
        lot = session.scalars(select(PharmacyLot).where(
            PharmacyLot.lot_number == ref_id)).first()
        if lot is None:
            raise HTTPException(
                status_code=404,
                detail=f"No dispense or lot '{ref_id}' for a medication "
                       "label")
        lines = [f"Medication code: {lot.medication_code}",
                 f"Lot: {lot.lot_number}", f"Expiry: {lot.expiry_date}",
                 f"Location: {lot.location}"]
        return "Medication lot label", lines, value_or(lot.lot_number)
    if kind == "asset":
        device = session.scalars(select(Device).where(
            Device.device_key == ref_id)).first()
        if device is None:
            raise HTTPException(status_code=404,
                                detail=f"Device/{ref_id} not found")
        prefix = (_kind_cfg(standards, "asset") or {}).get("prefix",
                                                           "OCAS1")
        lines = [f"Asset: {device.name}",
                 f"Type: {device.device_type}",
                 f"Location: {device.location}",
                 f"Serial: {device.serial}"]
        return "Asset label", lines, value_or(
            f"{prefix}{separator}{ref_id}")
    raise HTTPException(status_code=422,
                        detail=f"No label for kind '{kind}'")


@router.get("/labels/{kind}/{entity_ref:path}")
def print_label(kind: str, entity_ref: str,
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("barcode.read"))):
    """Render one print-ready label PDF. Every render is audit-chained
    with the Phase 13 print-confidentiality action
    (`reports.document.print`)."""
    if kind not in KINDS:
        raise HTTPException(status_code=422,
                            detail=f"'kind' must be one of {list(KINDS)}")
    title, lines, barcode_value = _label_content(session, kind, entity_ref)
    content = _label_pdf(_print_template(session), kind, title, lines,
                         barcode_value)
    record = session.scalars(select(BarcodeRecord).where(
        BarcodeRecord.value == barcode_value)).first()
    if record is not None:
        _add_event(session, barcode_value, "label-printed", user.username,
                   {"kind": kind, "entity_ref": entity_ref})
    append_audit(session, actor=user.username,
                 action="reports.document.print",
                 entity=f"document:barcode-label-{kind}",
                 payload={"document": f"barcode-label-{kind}",
                          "reference": entity_ref,
                          "barcode_value": barcode_value,
                          "format": "pdf"})
    append_audit(session, actor=user.username, action="barcodes.label.print",
                 entity=f"Barcode/{barcode_value}",
                 payload={"kind": kind, "entity_ref": entity_ref})
    session.commit()
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="label-{kind}.pdf"'})


# ---------------------------------------------------------------------------
# Seeding — published standards + guarded demo barcodes
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


def _register_demo(session: Session, standards: dict, kind: str,
                   entity_ref: str, value: str) -> None:
    """Register one demo barcode IF its value validates and is not
    already registered (idempotent; the entity-exists guard lives in
    the caller)."""
    if validate_value(standards, kind, value) is not None:
        return
    if session.scalars(select(BarcodeRecord).where(
            BarcodeRecord.value == value)).first() is not None:
        return
    session.add(BarcodeRecord(value=value, kind=kind,
                              entity_ref=entity_ref, label_fields={},
                              status="active", issued_by="system"))
    session.flush()
    _add_event(session, value, "registered", "system",
               {"kind": kind, "entity_ref": entity_ref, "demo": True})
    append_audit(session, actor="system", action="barcodes.register",
                 entity=f"Barcode/{value}",
                 payload={"kind": kind, "entity_ref": entity_ref,
                          "seeded_demo": True})


def ensure_barcodes_defaults(session: Session) -> None:
    """Seed the published barcode standards (idempotent), then demo
    barcodes — ONLY for entities that already exist (a fresh test
    database has no patients/specimens/units/dispenses at startup, so
    nothing is seeded there)."""
    _seed_published(session, "integration", STANDARDS_KEY,
                    default_standards_payload())
    session.flush()
    standards = _standards(session)
    separator = str(standards.get("separator") or "|")
    patient = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Patient")
        .order_by(FhirResource.id.asc())).first()
    if patient is not None:
        body = patient.body or {}
        dob_digits = str(body.get("birthDate") or "").replace("-", "")
        prefix = (_kind_cfg(standards, "wristband") or {}).get("prefix",
                                                               "OCWB1")
        if len(dob_digits) == 8 and dob_digits.isdigit():
            _register_demo(session, standards, "wristband",
                           f"Patient/{patient.resource_id}",
                           f"{prefix}{separator}{patient.resource_id}"
                           f"{separator}{dob_digits}")
    specimen = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Specimen")
        .order_by(FhirResource.id.asc())).first()
    if specimen is not None:
        prefix = (_kind_cfg(standards, "specimen") or {}).get("prefix",
                                                              "OCSP1")
        _register_demo(session, standards, "specimen",
                       f"Specimen/{specimen.resource_id}",
                       f"{prefix}{separator}{specimen.resource_id}")
    unit = session.scalars(
        select(BloodUnit).order_by(BloodUnit.id.asc())).first()
    if unit is not None:
        # Blood values ARE the ISBT 128 DINs; a demo unit whose id is
        # not a DIN-shaped value is skipped, never relabelled.
        _register_demo(session, standards, "blood",
                       f"BloodUnit/{unit.unit_id}", unit.unit_id)
    dispense = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "MedicationDispense")
        .order_by(FhirResource.id.asc())).first()
    if dispense is not None:
        prefix = (_kind_cfg(standards, "medication") or {}).get("prefix",
                                                                "OCML1")
        _register_demo(session, standards, "medication",
                       f"MedicationDispense/{dispense.resource_id}",
                       f"{prefix}{separator}{dispense.resource_id}"
                       f"{separator}-")
    session.commit()
