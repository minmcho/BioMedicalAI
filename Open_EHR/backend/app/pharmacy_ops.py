"""Phase 11 — Pharmacy department operations (Track C exemplar) + the
BCMA slice of Track G.

Phase 4A (app.pharmacy) made dispensing safe: formulary-gated, stock-
controlled, BCMA-lite. This module makes the pharmacy OPERABLE as a
department, on the Phase 10 platform (governed config, rules engine,
compliance register):

- **Inventory**: lot-level stock by location (app.models.PharmacyLot)
  under the Phase 4A aggregate counter — the two are kept equal for
  stock-tracked medications by construction (receiving, FEFO dispense
  allocation in app.pharmacy, adjustment and witnessed waste all move
  both in one transaction). PAR levels live in the published
  `terminology/pharmacy-ops` config; low-stock and expiry worklists
  are computed from lots + PAR, never stored.
- **Procurement**: purchase orders with cumulative receiving that can
  never over-receive; receiving captures the DSCSA transaction data
  (lot, serials, expiry per line) into an append-only traceability
  ledger (app.models.DscsaLedgerEntry).
- **ADC restock lists**: top-up-to-PAR per cabinet location, with the
  recent dispense count as context.
- **Verification + DUR work queues**: one pharmacist work centre.
  The verification queue is derived (signed/active medication orders
  with no PharmacyVerification row), ordered by clinical priority
  then wait, with SLA thresholds from config and breaches flagged.
  Verify is a clinical sign-off (`pharmacy.verify`): formulary-
  restricted items require an explicit restriction acknowledgement;
  non-formulary items cannot be verified. Verifying also emits the
  rules-engine event `pharmacy.dur.screened` with the patient's
  Cockcroft-Gault creatinine clearance — the seeded
  `renal-dose-review` rule turns a below-threshold CrCl into a
  pharmacist Task (Phase 10 engine, threshold in rule config).
- **Drug-drug interaction checking is bring-your-own-source
  (Phase 17, BD-70).** The Phase 11 evaluation
  (docs/sqa/PHASE11_SQA_REPORT.md) found no interaction knowledge
  source that is openly licensed for redistribution in software AND
  structured AND clinically governed: the NLM RxNav interaction
  API was discontinued 2024-01-02, DrugBank's severity data is
  commercially licensed, DDInter is an academic dataset without a
  redistribution license or RxNorm keys, openFDA is adverse-event
  reports, not pairwise interactions. Phase 17 therefore builds
  exactly the bring-your-own-licensed-source integration and
  nothing more: a deployer-supplied, licensed interaction source
  named in the published pharmacy-ops config slot
  (`interactionKnowledgeSource` — an object carrying the source
  name, a file/URL reference, a format declaration and a licence
  attestation: licence holder) is loaded by the governed loader
  below into a queryable pair index; loading is audited, and a
  source that is malformed or lacks the licence attestation is
  REFUSED (checking stays inert, with the refusal stated). Pair
  checking runs against the loaded source in the verification /
  DUR surfaces and at CPOE order placement (app.orders), always
  with severity + source attribution. Inventing, scraping or
  synthesising interaction data is prohibited: NO source ships
  with this system, the seeded config keeps the slot null, and
  with no source supplied the endpoints keep returning the
  explicit no-source status — the feature is never described as
  working without a procured licensed source.
- **Controlled substances**: two-person shift counts (expected vs
  counted, witness != counter), witnessed waste, and discrepancy
  records that must be resolved with a reason before the count book
  for that medication+location balances. Every movement is written
  to the hash-chained audit.
- **Sterile compounding / IV room**: batch records on a queue
  separate from verification; ingredients capture lots; the
  beyond-use datetime is computed from the published USP <797>
  category config (never a code constant); release requires a
  verifier who is not the preparer.
- **BCMA (Track G slice)**: barcode payload standards are config
  data (prefixes in the pharmacy-ops config); payloads are
  `PREFIX|field|field|check-digits` strings a USB wedge scanner
  types like a keyboard. `POST /pharmacy/bcma/administer` runs the
  five-rights check — patient, medication, dose, route, time —
  against the order behind the scanned dispense. Identity failures
  (wrong patient, wrong package/lot, wrong drug) are absolute hard
  stops. Dose/route/time failures are hard stops that a clinician
  may override ONLY with a reason, which is recorded on the
  administration and in the audit chain. Every refused scan is
  audited too: the chain of custody includes the refusals.

Wave A (PHARM-B1 / M1 / M2) closes the verification chain end to
end: verification is a CURRENT-ness state (active, bound to the
order's version, inside the governed validity window) computed by
`verification_state` / `current_verification` in this module;
dispensing (app.pharmacy) refuses 409 without one; verification
itself hard-stops on allergy conflicts, monitoring blockers and
high-alert medications unless a governed override or a recorded
DUR disposition covers the signal; verifications can be revoked
(reason required, audited); and this BCMA path — now the ONLY
administration path, the Phase 4A BCMA-lite endpoint is retired —
requires the authorizing order's verification to still be current
at administration time.
"""
import json
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from . import fhir, orders, pharmacy
from .audit import append_audit
from .configstore import published_config
from .models import (DscsaLedgerEntry, FhirResource,
                     PharmacyCompoundBatch, PharmacyCsCount,
                     PharmacyCsWaste, PharmacyDurDisposition, PharmacyLot,
                     PharmacyPurchaseOrder, PharmacyStock,
                     PharmacyVerification, User)
from .rules import evaluate_rules
from .security import get_db, require_capability

router = APIRouter(prefix="/pharmacy", tags=["pharmacy-ops"])

OPS_KEY = pharmacy.OPS_KEY
BCMA_OVERRIDE_EXTENSION = "urn:opencare:extension/bcma-override"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Published operations config (terminology/pharmacy-ops)
# ---------------------------------------------------------------------------

def default_ops_payload() -> dict:
    """Synthetic pharmacy-operations defaults. Everything a site tunes
    about HOW the department runs — locations, PAR levels, SLA
    thresholds, USP <797> beyond-use categories, barcode prefixes,
    the interaction knowledge-source slot — is this config object,
    not code (standing rule: configure, don't fork)."""
    def par(code, location, par_level, reorder):
        return {"medicationCode": code, "location": location,
                "parLevel": par_level, "reorderPoint": reorder}

    return {
        "name": "Pharmacy operations (synthetic defaults)",
        "locations": [
            {"code": "main-pharmacy", "display": "Main pharmacy"},
            {"code": "iv-room", "display": "IV room — sterile compounding"},
            {"code": "adc-icu", "display": "ADC — ICU"},
            {"code": "adc-med-surg", "display": "ADC — Med/Surg"},
        ],
        "defaultDispenseLocation": "main-pharmacy",
        "expiryWarningDays": 30,
        "verificationSlaMinutes": {"stat": 30, "asap": 60, "urgent": 120,
                                   "routine": 480},
        # Wave A (PHARM-B1): verification currency window in hours.
        # null = verifications do not expire by time (order-version
        # binding + revocation still apply); a site publishes a
        # number to make stale-by-age verifications non-current.
        "verificationValidityHours": None,
        # Wave A (PHARM-M1): the governed DUR override reason codes.
        # Verification may proceed past a hard-stop signal (allergy
        # conflict, monitoring blocker, high-alert medication) only
        # with one of these codes + a note, recorded on the
        # verification; DUR dispositions use the same code list.
        "durOverrideReasonCodes": [
            "benefit-outweighs-risk",
            "allergy-cross-reactivity-assessed",
            "monitoring-plan-in-place",
            "specialist-direction",
            "patient-previously-tolerated",
        ],
        "parLevels": [
            par("1049221", "main-pharmacy", 100, 40),
            par("847232", "main-pharmacy", 150, 60),
            par("1044910", "main-pharmacy", 80, 30),
            par("1048467", "main-pharmacy", 50, 20),
            par("1351886", "main-pharmacy", 40, 15),
            par("1049625", "main-pharmacy", 30, 10),
            par("197589", "main-pharmacy", 40, 15),
            par("1049221", "adc-icu", 20, 8),
            par("847232", "adc-icu", 20, 8),
            par("1049221", "adc-med-surg", 12, 5),
        ],
        "adcUsageWindowDays": 7,
        "compounding": {
            # USP <797> beyond-use dating AS CONFIGURATION: hours per
            # category per storage condition. Category 1 carries the
            # chapter's limits (12 h room / 24 h refrigerated / 45 d
            # frozen); Category 2 values are site EXAMPLE values —
            # the chapter's table depends on the preparation's
            # conditions, and the site's adopted values are published
            # here by its pharmacy, not asserted by this code.
            "categories": [
                {"code": "category-1", "display": "USP <797> Category 1",
                 "budHours": {"room": 12, "refrigerated": 24, "frozen": 1080}},
                {"code": "category-2",
                 "display": "USP <797> Category 2 (site example values)",
                 "budHours": {"room": 720, "refrigerated": 1080,
                              "frozen": 2160}},
            ],
            "storageConditions": ["room", "refrigerated", "frozen"],
        },
        "barcodeStandards": {
            "wristbandPrefix": "OCWB1",
            "medLabelPrefix": "OCML1",
            "separator": "|",
        },
        # Drug-interaction knowledge source slot: null = none attached,
        # checking inert (see module docstring). A deployer attaches
        # ITS OWN licensed source by publishing an object here:
        # {name, reference (file path / file:// / http(s) URL),
        # format, licenceHolder} — the Phase 17 governed loader
        # (below) loads it, audits the load, and refuses any source
        # without the licence attestation. No source ships.
        "interactionKnowledgeSource": None,
    }


def _ops(session: Session) -> dict:
    payload = pharmacy._ops_payload(session)
    return payload if payload else default_ops_payload()


def _location_codes(ops: dict) -> list[str]:
    return [loc["code"] for loc in ops.get("locations") or []
            if isinstance(loc, dict) and loc.get("code")]


def _par_map(ops: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for entry in ops.get("parLevels") or []:
        if isinstance(entry, dict) and entry.get("medicationCode") \
                and entry.get("location"):
            out[(str(entry["medicationCode"]), str(entry["location"]))] = entry
    return out


def _formulary_map(session: Session) -> dict[str, dict]:
    return {str(c["code"]): c
            for c in pharmacy._formulary(session).get("concepts") or []
            if isinstance(c, dict) and c.get("code")}


def _lot_qty(session: Session, code: str, location: str) -> int:
    return sum(l.quantity for l in session.scalars(
        select(PharmacyLot).where(PharmacyLot.medication_code == code,
                                  PharmacyLot.location == location)).all())


def _days_to_expiry(lot: PharmacyLot) -> int | None:
    try:
        expiry = date.fromisoformat(lot.expiry_date)
    except (ValueError, TypeError):
        return None
    return (expiry - date.today()).days


def ensure_pharmacy_ops_defaults(session: Session) -> None:
    """Seed the pharmacy-ops config (published v1, like the formulary)
    and the opening lot inventory: for every aggregate stock balance
    with no lots yet, one seed lot at the default dispense location
    expiring in 12 months — so the lot book and the aggregate agree
    from the first boot (idempotent: existing lots are the truth)."""
    pharmacy._seed_published(session, "terminology", OPS_KEY,
                             default_ops_payload())
    ops = _ops(session)
    location = ops.get("defaultDispenseLocation") or "main-pharmacy"
    expiry = (date.today() + timedelta(days=365)).isoformat()
    for row in session.scalars(select(PharmacyStock)).all():
        if row.quantity <= 0:
            continue
        existing = session.scalars(
            select(PharmacyLot).where(
                PharmacyLot.medication_code == row.medication_code)).first()
        if existing is None:
            session.add(PharmacyLot(
                medication_code=row.medication_code, location=location,
                lot_number=f"SEED-{row.medication_code}",
                expiry_date=expiry, quantity=row.quantity))
    session.commit()


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


def _require_int(payload: dict, field: str, minimum: int) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' must be an integer >= {minimum}")
    return value


def _require_str(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' is required")
    return value.strip()


# ---------------------------------------------------------------------------
# Inventory — lots by location, worklists, adjustments
# ---------------------------------------------------------------------------

def _lot_out(session: Session, lot: PharmacyLot, ops: dict) -> dict:
    days = _days_to_expiry(lot)
    warn = ops.get("expiryWarningDays")
    warn = warn if isinstance(warn, (int, float)) and not isinstance(warn, bool) else 30
    entry = _par_map(ops).get((lot.medication_code, lot.location), {})
    return {
        "medication_code": lot.medication_code,
        "location": lot.location,
        "lot_number": lot.lot_number,
        "expiry_date": lot.expiry_date,
        "quantity": lot.quantity,
        "days_to_expiry": days,
        "expired": days is not None and days < 0,
        "expiring_soon": days is not None and 0 <= days <= warn,
        "par_level": entry.get("parLevel"),
        "reorder_point": entry.get("reorderPoint"),
    }


@router.get("/inventory")
def get_inventory(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("pharmacy.read"))):
    """Lot-level stock: every lot at every location with its expiry
    posture and the PAR context from published config."""
    ops = _ops(session)
    lots = session.scalars(
        select(PharmacyLot).order_by(PharmacyLot.medication_code.asc(),
                                     PharmacyLot.location.asc(),
                                     PharmacyLot.expiry_date.asc(),
                                     PharmacyLot.id.asc())).all()
    return {"locations": ops.get("locations") or [],
            "items": [_lot_out(session, lot, ops) for lot in lots],
            "total": len(lots)}


@router.get("/inventory/worklist")
def inventory_worklist(session: Session = Depends(get_db),
                       user: User = Depends(require_capability("pharmacy.read"))):
    """The buyer's morning list, computed: locations whose on-hand has
    fallen below the configured reorder point (with the order-up-to-PAR
    suggestion), lots expiring inside the warning window, and lots
    already expired (which FEFO allocation will never pick while any
    in-date lot covers a dispense — and which the department must
    pull)."""
    ops = _ops(session)
    formulary = _formulary_map(session)
    low_stock = []
    for (code, location), entry in sorted(_par_map(ops).items()):
        reorder = entry.get("reorderPoint")
        if not isinstance(reorder, (int, float)) or isinstance(reorder, bool):
            continue
        on_hand = _lot_qty(session, code, location)
        if on_hand < reorder:
            par_level = entry.get("parLevel")
            low_stock.append({
                "medication_code": code,
                "display": (formulary.get(code) or {}).get("display"),
                "location": location, "on_hand": on_hand,
                "reorder_point": reorder,
                "par_level": par_level,
                "suggested_order": (par_level - on_hand)
                if isinstance(par_level, (int, float))
                and not isinstance(par_level, bool) else None,
            })
    lots = session.scalars(select(PharmacyLot).where(
        PharmacyLot.quantity > 0)).all()
    expiring, expired = [], []
    for lot in lots:
        days = _days_to_expiry(lot)
        if days is None:
            continue
        out = _lot_out(session, lot, ops)
        if days < 0:
            expired.append(out)
        elif out["expiring_soon"]:
            expiring.append(out)
    return {"low_stock": low_stock, "expiring": expiring,
            "expired": expired,
            "expiry_warning_days": ops.get("expiryWarningDays")}


@router.post("/inventory/adjust", status_code=201)
async def adjust_inventory(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("pharmacy.inventory"))):
    """A manual stock adjustment against ONE lot (breakage, count
    correction): reason mandatory, result can never go negative, the
    aggregate counter moves by the same delta, and the adjustment is
    hash-chain audited with before/after. New lots arrive through
    receiving, never through adjustment."""
    payload = await _json_body(request)
    code = _require_str(payload, "medication_code")
    location = _require_str(payload, "location")
    lot_number = _require_str(payload, "lot_number")
    reason = _require_str(payload, "reason")
    delta = payload.get("delta")
    if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
        raise HTTPException(status_code=422,
                            detail="'delta' must be a non-zero integer")
    entry = pharmacy._formulary_entry(session, code)
    if entry is None:
        raise HTTPException(
            status_code=422,
            detail=f"Medication code '{code}' is not in the published formulary")
    lot = session.scalars(
        select(PharmacyLot).where(PharmacyLot.medication_code == code,
                                  PharmacyLot.location == location,
                                  PharmacyLot.lot_number == lot_number)).first()
    if lot is None:
        raise HTTPException(
            status_code=422,
            detail=(f"No lot '{lot_number}' of '{code}' at '{location}'; "
                    "stock arrives through procurement receiving"))
    before = lot.quantity
    after = before + delta
    if after < 0:
        raise HTTPException(
            status_code=409,
            detail=(f"Adjustment of {delta} would take lot '{lot_number}' "
                    f"below zero ({before} on hand)"))
    lot.quantity = after
    stock_after = None
    if entry.get("stockTracked"):
        stock_row = pharmacy._stock_row(session, code, entry)
        stock_row.quantity += delta
        stock_after = stock_row.quantity
    session.flush()
    append_audit(session, actor=user.username,
                 action="pharmacy.inventory.adjust",
                 entity=f"PharmacyLot/{code}/{location}/{lot_number}",
                 payload={"medication_code": code, "location": location,
                          "lot_number": lot_number, "delta": delta,
                          "before": before, "after": after,
                          "stock_after": stock_after, "reason": reason})
    session.commit()
    return {"lot": _lot_out(session, lot, _ops(session)),
            "stock_after": stock_after}


# ---------------------------------------------------------------------------
# Procurement — purchase orders, receiving, DSCSA traceability ledger
# ---------------------------------------------------------------------------

def _po_out(po: PharmacyPurchaseOrder) -> dict:
    return {"po_id": po.po_id, "supplier": po.supplier, "status": po.status,
            "lines": po.lines or [], "note": po.note,
            "created_by": po.created_by,
            "created_at": _aware(po.created_at).isoformat()
            if po.created_at else None}


@router.get("/procurement/orders")
def list_purchase_orders(session: Session = Depends(get_db),
                         user: User = Depends(require_capability("pharmacy.read"))):
    rows = session.scalars(
        select(PharmacyPurchaseOrder).order_by(
            PharmacyPurchaseOrder.id.desc())).all()
    return {"items": [_po_out(po) for po in rows], "total": len(rows)}


@router.post("/procurement/orders", status_code=201)
async def create_purchase_order(request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(require_capability("pharmacy.inventory"))):
    payload = await _json_body(request)
    supplier = _require_str(payload, "supplier")
    lines_in = payload.get("lines")
    if not isinstance(lines_in, list) or not lines_in:
        raise HTTPException(status_code=422,
                            detail="'lines' must be a non-empty list")
    lines = []
    for i, line in enumerate(lines_in):
        if not isinstance(line, dict):
            raise HTTPException(status_code=422,
                                detail=f"lines[{i}] must be an object")
        code = _require_str(line, "medication_code")
        if pharmacy._formulary_entry(session, code) is None:
            raise HTTPException(
                status_code=422,
                detail=f"lines[{i}]: medication code '{code}' is not in "
                       "the published formulary")
        quantity = line.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) \
                or quantity < 1:
            raise HTTPException(
                status_code=422,
                detail=f"lines[{i}]: 'quantity' must be a positive integer")
        lines.append({"medication_code": code, "quantity": quantity,
                      "received_quantity": 0})
    po = PharmacyPurchaseOrder(po_id=f"po-{uuid.uuid4()}", supplier=supplier,
                               status="ordered", lines=lines,
                               note=payload.get("note")
                               if isinstance(payload.get("note"), str) else None,
                               created_by=user.username)
    session.add(po)
    session.flush()
    append_audit(session, actor=user.username,
                 action="pharmacy.procurement.create",
                 entity=f"PharmacyPurchaseOrder/{po.po_id}",
                 payload={"supplier": supplier, "lines": lines})
    session.commit()
    return _po_out(po)


@router.post("/procurement/orders/{po_id}/receive", status_code=201)
async def receive_purchase_order(po_id: str, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability("pharmacy.inventory"))):
    """Receive against a PO. Every line captures its DSCSA transaction
    data — lot, expiry, and the unit serials when the shipment carries
    them (a serial list whose length is not the received quantity is
    refused). Receiving is validated in full BEFORE anything is
    written: an over-receive or a bad line changes nothing."""
    payload = await _json_body(request)
    po = session.scalars(select(PharmacyPurchaseOrder).where(
        PharmacyPurchaseOrder.po_id == po_id)).first()
    if po is None:
        raise HTTPException(status_code=404,
                            detail=f"Purchase order '{po_id}' was not found")
    if po.status == "received":
        raise HTTPException(status_code=409,
                            detail=f"Purchase order '{po_id}' is fully received")
    ops = _ops(session)
    location = payload.get("location") or ops.get("defaultDispenseLocation") \
        or "main-pharmacy"
    if location not in _location_codes(ops):
        raise HTTPException(status_code=422,
                            detail=f"Unknown stock location '{location}'")
    lines_in = payload.get("lines")
    if not isinstance(lines_in, list) or not lines_in:
        raise HTTPException(status_code=422,
                            detail="'lines' must be a non-empty list")
    po_lines = {line["medication_code"]: line for line in po.lines or []}
    # ---- validation pass (nothing written yet) ----
    prepared = []
    for i, line in enumerate(lines_in):
        if not isinstance(line, dict):
            raise HTTPException(status_code=422,
                                detail=f"lines[{i}] must be an object")
        code = _require_str(line, "medication_code")
        lot_number = _require_str(line, "lot_number")
        expiry = _require_str(line, "expiry_date")
        try:
            date.fromisoformat(expiry)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"lines[{i}]: 'expiry_date' must be an ISO date")
        quantity = line.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) \
                or quantity < 1:
            raise HTTPException(
                status_code=422,
                detail=f"lines[{i}]: 'quantity' must be a positive integer")
        serials = line.get("serials")
        if serials is not None:
            if not isinstance(serials, list) \
                    or any(not isinstance(s, str) or not s for s in serials):
                raise HTTPException(
                    status_code=422,
                    detail=f"lines[{i}]: 'serials' must be a list of strings")
            if len(serials) != quantity:
                raise HTTPException(
                    status_code=422,
                    detail=(f"lines[{i}]: {len(serials)} serials for "
                            f"{quantity} units — DSCSA serial capture must "
                            "cover every unit received"))
        po_line = po_lines.get(code)
        if po_line is None:
            raise HTTPException(
                status_code=422,
                detail=f"lines[{i}]: '{code}' is not on purchase order {po_id}")
        if po_line["received_quantity"] + quantity > po_line["quantity"]:
            raise HTTPException(
                status_code=422,
                detail=(f"lines[{i}]: receiving {quantity} of '{code}' "
                        f"would exceed the ordered {po_line['quantity']} "
                        f"({po_line['received_quantity']} already received)"))
        existing_lot = session.scalars(
            select(PharmacyLot).where(
                PharmacyLot.medication_code == code,
                PharmacyLot.location == location,
                PharmacyLot.lot_number == lot_number)).first()
        if existing_lot is not None and existing_lot.expiry_date != expiry:
            raise HTTPException(
                status_code=422,
                detail=(f"lines[{i}]: lot '{lot_number}' already exists at "
                        f"{location} with expiry {existing_lot.expiry_date}, "
                        f"not {expiry}"))
        prepared.append((code, lot_number, expiry, quantity, serials,
                         po_line, existing_lot))
    # ---- apply pass ----
    received_summary = []
    for code, lot_number, expiry, quantity, serials, po_line, lot in prepared:
        if lot is None:
            lot = PharmacyLot(medication_code=code, location=location,
                              lot_number=lot_number, expiry_date=expiry,
                              quantity=0)
            session.add(lot)
            session.flush()
        lot.quantity += quantity
        entry = pharmacy._formulary_entry(session, code)
        if entry is not None and entry.get("stockTracked"):
            pharmacy._stock_row(session, code, entry).quantity += quantity
        po_line["received_quantity"] += quantity
        session.add(DscsaLedgerEntry(
            entry_id=f"dscsa-{uuid.uuid4()}", event="receiving",
            medication_code=code, lot_number=lot_number,
            serial=serials[0] if serials else None, expiry_date=expiry,
            quantity=quantity, reference=po_id,
            detail={"serials": serials or [], "supplier": po.supplier,
                    "location": location},
            actor=user.username))
        received_summary.append({"medication_code": code,
                                 "lot_number": lot_number,
                                 "quantity": quantity})
    po.lines = list(po.lines or [])
    # The increments above mutated the JSON lines in place; a shallow
    # copy compares equal to the stored value, so SQLAlchemy would
    # skip the UPDATE without an explicit dirty flag.
    flag_modified(po, "lines")
    if all(line["received_quantity"] >= line["quantity"] for line in po.lines):
        po.status = "received"
    else:
        po.status = "partially_received"
    session.flush()
    append_audit(session, actor=user.username,
                 action="pharmacy.procurement.receive",
                 entity=f"PharmacyPurchaseOrder/{po_id}",
                 payload={"location": location, "lines": received_summary,
                          "status": po.status})
    session.commit()
    return _po_out(po)


@router.get("/procurement/traceability")
def get_traceability(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("pharmacy.read"))):
    """The DSCSA ledger: every receiving and dispense traceability
    event, newest first — the queryable answer to "where did this
    lot / serial go"."""
    rows = session.scalars(
        select(DscsaLedgerEntry).order_by(DscsaLedgerEntry.id.desc())
        .limit(200)).all()
    return {"items": [{
        "entry_id": r.entry_id, "event": r.event,
        "medication_code": r.medication_code, "lot_number": r.lot_number,
        "serial": r.serial, "expiry_date": r.expiry_date,
        "quantity": r.quantity, "reference": r.reference,
        "detail": r.detail or {}, "actor": r.actor,
        "created_at": _aware(r.created_at).isoformat() if r.created_at else None,
    } for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# ADC restock lists
# ---------------------------------------------------------------------------

@router.get("/adc/restock-list")
def adc_restock_list(location: str = Query(...),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("pharmacy.read"))):
    """Restock list for one automated dispensing cabinet location:
    every PAR item below its PAR level, topped up to PAR, with the
    medication's recent (config-window) dispense count as the demand
    context a pharmacist sanity-checks the suggestion against."""
    ops = _ops(session)
    if location not in _location_codes(ops):
        raise HTTPException(status_code=422,
                            detail=f"Unknown stock location '{location}'")
    formulary = _formulary_map(session)
    window_days = ops.get("adcUsageWindowDays")
    window_days = window_days if isinstance(window_days, (int, float)) \
        and not isinstance(window_days, bool) and window_days > 0 else 7
    cutoff = _now() - timedelta(days=float(window_days))
    usage: dict[str, int] = {}
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "MedicationDispense")).all():
        created = _aware(row.created_at)
        if created is not None and created >= cutoff:
            _, code, _ = pharmacy._medication_of(row.body or {})
            if code:
                usage[code] = usage.get(code, 0) + 1
    items = []
    for (code, loc), entry in sorted(_par_map(ops).items()):
        if loc != location:
            continue
        par_level = entry.get("parLevel")
        if not isinstance(par_level, (int, float)) \
                or isinstance(par_level, bool):
            continue
        on_hand = _lot_qty(session, code, location)
        if on_hand < par_level:
            items.append({
                "medication_code": code,
                "display": (formulary.get(code) or {}).get("display"),
                "location": location, "on_hand": on_hand,
                "par_level": par_level,
                "suggested_quantity": par_level - on_hand,
                "recent_dispenses": usage.get(code, 0),
            })
    return {"location": location, "window_days": window_days, "items": items}


# ---------------------------------------------------------------------------
# Verification queue, DUR queue, interaction-check integration point
# ---------------------------------------------------------------------------

PRIORITY_RANK = {"stat": 0, "asap": 1, "urgent": 2, "routine": 3}
CREATININE_LOINC = "2160-0"   # Creatinine [Mass/volume] in Serum or Plasma
WEIGHT_LOINC = "29463-7"      # Body weight


def _open_med_orders(session: Session) -> list[FhirResource]:
    """Medication orders a pharmacist can still act on: signed or
    active (the order-flow fulfilment states), oldest first."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "MedicationRequest")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    return [r for r in rows
            if (r.body or {}).get("status") in ("signed", "active")]


# ---------------------------------------------------------------------------
# Current verification (Wave A, PHARM-B1) — the single source every
# consumer (dispense gate, BCMA gate, verification queue, DUR queue,
# home card) reads. A verification is CURRENT iff:
#   1. a PharmacyVerification row exists for the order, AND
#   2. its status is "active" (not revoked), AND
#   3. its order_version equals the MedicationRequest's current
#      FhirResource.version — an edited order invalidates the
#      verification (stale) until it is re-verified. (Dispensing
#      re-binds the verification across the dispense's own status
#      transition — see app.pharmacy.dispense — so a lawful dispense
#      does not stale the verification it was authorized by; any
#      other edit still does.) AND
#   4. verified_at is inside the governed validity window: the
#      pharmacy-ops config `verificationValidityHours`; null / absent
#      / non-numeric means NO time expiry.
# States: current | none | revoked | stale | expired, each with a
# human reason that refusal messages quote verbatim.
# ---------------------------------------------------------------------------

VERIFICATION_ADD_COLUMNS = (
    ("status", "VARCHAR(20) NOT NULL DEFAULT 'active'"),
    ("revoked_by", "VARCHAR(120)"),
    ("revoked_at", "DATETIME"),
    ("revoke_reason", "TEXT"),
    ("order_version", "INTEGER"),
    ("override_reason_code", "VARCHAR(120)"),
    ("override_note", "TEXT"),
    ("overridden_signals", "TEXT"),
)


def ensure_pharmacy_verification_columns(engine) -> None:
    """Additive Wave A columns for pre-existing databases (the
    org_hierarchy.ensure_org_columns precedent: create_all never
    alters an existing table). Inspector-guarded ALTER TABLE ADD
    COLUMN for exactly the Wave A columns on pharmacy_verifications;
    a no-op wherever they already exist (every fresh database).
    COORDINATOR WIRING: call from app.main.create_app next to
    org_hierarchy.ensure_org_columns(engine)."""
    inspector = inspect(engine)
    if "pharmacy_verifications" not in set(inspector.get_table_names()):
        return
    present = {c["name"] for c in
               inspector.get_columns("pharmacy_verifications")}
    with engine.begin() as conn:
        for column, ddl in VERIFICATION_ADD_COLUMNS:
            if column not in present:
                conn.execute(text(
                    f"ALTER TABLE pharmacy_verifications "
                    f"ADD COLUMN {column} {ddl}"))


def _verification_validity_hours(session: Session) -> float | None:
    value = _ops(session).get("verificationValidityHours")
    if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and value > 0:
        return float(value)
    return None


def _evaluate_verification(record: PharmacyVerification | None,
                           order_version: int | None,
                           validity_hours: float | None,
                           now: datetime) -> tuple[str, str]:
    """(state, reason) for one verification row against the order's
    current version — the pure core of verification_state."""
    if record is None:
        return "none", "no verification on file"
    status = getattr(record, "status", None) or "active"
    if status == "revoked":
        by = getattr(record, "revoked_by", None) or "unknown"
        why = getattr(record, "revoke_reason", None) or "no reason recorded"
        return "revoked", f"verification revoked by {by} (reason: {why})"
    if status != "active":
        return "revoked", f"verification is not active (status '{status}')"
    verified_version = getattr(record, "order_version", None)
    if order_version is None:
        return "stale", ("order changed since verification — re-verify "
                         "(the order no longer exists to bind against)")
    if verified_version != order_version:
        return "stale", (
            "order changed since verification — re-verify "
            f"(verification was recorded at order version "
            f"{verified_version}; the order is now at version "
            f"{order_version})")
    if validity_hours is not None:
        verified_at = _aware(record.verified_at)
        if verified_at is not None \
                and now > verified_at + timedelta(hours=validity_hours):
            return "expired", (
                f"verification expired — verified at "
                f"{verified_at.isoformat()} is outside the governed "
                f"validity window of {validity_hours:g} hours "
                "(pharmacy-ops verificationValidityHours)")
    return "current", "current verification on file"


def verification_state(session: Session, order_id: str,
                       record: PharmacyVerification | None = None,
                       order_row: FhirResource | None = None) -> dict:
    """The Wave A verification state of one order:
    {state, reason, verification (row|None), order_version}.
    `record` / `order_row` may be supplied by callers that already
    hold them; otherwise they are loaded here."""
    if record is None:
        record = session.scalars(
            select(PharmacyVerification).where(
                PharmacyVerification.order_id == order_id)).first()
    if record is None:
        return {"state": "none", "reason": "no verification on file",
                "verification": None, "order_version": None}
    if order_row is None:
        order_row = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "MedicationRequest",
                FhirResource.resource_id == order_id)).first()
    state, reason = _evaluate_verification(
        record, order_row.version if order_row is not None else None,
        _verification_validity_hours(session), _now())
    return {"state": state, "reason": reason, "verification": record,
            "order_version": order_row.version if order_row else None}


def current_verification(session: Session,
                         order_id: str) -> PharmacyVerification | None:
    """The order's CURRENT verification row, or None — the helper
    the dispense gate, the BCMA gate and the queues all consume."""
    state = verification_state(session, order_id)
    return state["verification"] if state["state"] == "current" else None


def _verified_order_ids(session: Session) -> set[str]:
    """Order ids whose verification is CURRENT (Wave A semantics —
    a revoked, stale or expired verification does NOT count, so the
    order returns to the verification queue and off the verified
    sets everywhere this helper feeds)."""
    records = session.scalars(select(PharmacyVerification)).all()
    if not records:
        return set()
    versions = {row.resource_id: row.version for row in session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "MedicationRequest")).all()}
    validity = _verification_validity_hours(session)
    now = _now()
    return {r.order_id for r in records
            if _evaluate_verification(
                r, versions.get(r.order_id), validity, now)[0] == "current"}


def _verification_out(record: PharmacyVerification) -> dict:
    """The API shape of a verification row (verify / revoke responses)."""
    overridden = None
    raw = getattr(record, "overridden_signals", None)
    if raw:
        try:
            overridden = json.loads(raw)
        except (ValueError, TypeError):
            overridden = None
    return {
        "order_id": record.order_id,
        "patient_ref": record.patient_ref,
        "medication_code": record.medication_code,
        "verified_by": record.verified_by,
        "verified_at": _aware(record.verified_at).isoformat()
        if record.verified_at else None,
        "status": getattr(record, "status", None) or "active",
        "restriction_acknowledged": record.restriction_acknowledged,
        "note": record.note,
        "order_version": getattr(record, "order_version", None),
        "override_reason_code": getattr(record, "override_reason_code", None),
        "override_note": getattr(record, "override_note", None),
        "overridden_signals": overridden,
        "revoked_by": getattr(record, "revoked_by", None),
        "revoked_at": _aware(record.revoked_at).isoformat()
        if getattr(record, "revoked_at", None) else None,
        "revoke_reason": getattr(record, "revoke_reason", None),
    }


def _creatinine_clearance(session: Session,
                          patient_ref: str) -> float | None:
    """Cockcroft-Gault estimate from data already in the chart: the
    latest RELEASED serum creatinine (LOINC 2160-0) and body weight
    (LOINC 29463-7) via the Phase 9 monitoring reader (unreleased lab
    drafts never count), plus the patient's birthDate and gender.
    Any missing input -> None: the flag simply does not compute, it
    is never estimated from nothing."""
    patient_id = patient_ref.split("/", 1)[1] if "/" in patient_ref \
        else patient_ref
    patient = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Patient",
            FhirResource.resource_id == patient_id)).first()
    if patient is None:
        return None
    body = patient.body or {}
    dob = pharmacy._parse_dt(body.get("birthDate"))
    scr = pharmacy._latest_result(session, patient_ref, CREATININE_LOINC)
    weight = pharmacy._latest_result(session, patient_ref, WEIGHT_LOINC)
    if dob is None or scr is None or weight is None:
        return None
    age_years = (_now() - dob).days / 365.25
    scr_value, weight_kg = scr[0], weight[0]
    if scr_value <= 0 or weight_kg <= 0 or age_years <= 0 or age_years >= 140:
        return None
    crcl = ((140.0 - age_years) * weight_kg) / (72.0 * scr_value)
    if str(body.get("gender") or "").lower() == "female":
        crcl *= 0.85
    return round(crcl, 1)


def _renal_threshold(session: Session) -> float | None:
    """The renal threshold is the seeded renal-dose-review RULE's
    condition value — one source of truth, in governed config. No
    published rule -> no threshold -> no flag."""
    cfg = published_config(session, "rule", "renal-dose-review")
    if cfg is None:
        return None
    cond = (cfg.payload or {}).get("condition") or {}
    value = cond.get("value")
    if cond.get("signal") == "crcl_ml_min" \
            and isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        return float(value)
    return None


def _renal_view(session: Session, patient_ref: str) -> dict:
    crcl = _creatinine_clearance(session, patient_ref)
    threshold = _renal_threshold(session)
    return {"crcl_ml_min": crcl, "threshold_ml_min": threshold,
            "flagged": crcl is not None and threshold is not None
            and crcl < threshold}


# ---------------------------------------------------------------------------
# DUR hard-stop signals + dispositions (Wave A, PHARM-M1)
#
# A hard-stop signal is a verification-time stop: a recorded-allergy
# conflict, a medication-monitoring BLOCKER (warnings never block),
# or a high-alert medication (formulary `highAlert: true`). Each
# signal carries a STABLE (signal_type, signal_key) pair, recomputed
# identically by the DUR queue, verify_order and the disposition
# endpoint, so a disposition recorded from the queue matches the
# signal verify recomputes:
#   allergy           -> key = the medication code
#   monitoring-block  -> key = the blocker lab code (or "weight-based")
#   high-alert        -> key = the medication code
# A recorded disposition of "override" for the pair satisfies that
# signal at verify time; any remaining signal needs the governed
# payload override (reason code from the published
# `durOverrideReasonCodes` + a non-empty note).
# ---------------------------------------------------------------------------

DUR_DISPOSITIONS = ("override", "order-changed", "therapy-changed",
                    "not-applicable")


def _dur_override_codes(ops: dict) -> list[str]:
    """The governed override reason codes from published config:
    entries may be plain code strings or {code, display} objects."""
    codes: list[str] = []
    for entry in ops.get("durOverrideReasonCodes") or []:
        if isinstance(entry, str) and entry.strip():
            codes.append(entry.strip())
        elif isinstance(entry, dict) and entry.get("code"):
            codes.append(str(entry["code"]))
    return codes


def _hard_stop_signals(session: Session, patient_ref: str, code: str,
                       entry: dict | None, allergy_conflict: dict | None,
                       blockers: list[dict]) -> list[dict]:
    """The verification hard stops for one order, in a stable order
    (allergy, monitoring blockers, high-alert)."""
    signals: list[dict] = []
    if allergy_conflict is not None:
        signals.append({
            "type": "allergy", "signal_key": str(code),
            "detail": (f"Recorded allergy to "
                       f"{allergy_conflict['display']} "
                       f"(AllergyIntolerance/"
                       f"{allergy_conflict['allergy_id']})")})
    for blocker in blockers:
        signals.append({
            "type": "monitoring-block",
            "signal_key": str(blocker.get("lab_code") or "weight-based"),
            "detail": blocker["problem"]})
    if entry is not None and entry.get("highAlert") is True:
        signals.append({
            "type": "high-alert", "signal_key": str(code),
            "detail": (f"{entry.get('display') or code} is a high-alert "
                       "medication (formulary highAlert)")})
    return signals


def _disposition_out(row: PharmacyDurDisposition) -> dict:
    return {"id": row.id, "order_id": row.order_id,
            "signal_type": row.signal_type, "signal_key": row.signal_key,
            "disposition": row.disposition, "reason_code": row.reason_code,
            "note": row.note, "by": row.by,
            "at": _aware(row.at).isoformat() if row.at else None}


def _dispositions_for(session: Session,
                      order_id: str) -> list[PharmacyDurDisposition]:
    return session.scalars(
        select(PharmacyDurDisposition).where(
            PharmacyDurDisposition.order_id == order_id)
        .order_by(PharmacyDurDisposition.id.asc())).all()


def _queue_item(session: Session, row: FhirResource, ops: dict) -> dict:
    body = row.body or {}
    _, code, display = orders._code_of(body, "medicationCodeableConcept")
    patient_ref = row.patient_ref or (
        (body.get("subject") or {}).get("reference") or "")
    priority = str(body.get("priority") or "routine").lower()
    authored = pharmacy._parse_dt(body.get("authoredOn")) \
        or _aware(row.created_at)
    wait_minutes = None
    if authored is not None:
        wait_minutes = max(0, int((_now() - authored).total_seconds() // 60))
    sla_map = ops.get("verificationSlaMinutes") or {}
    sla = sla_map.get(priority, sla_map.get("routine", 480))
    if not isinstance(sla, (int, float)) or isinstance(sla, bool):
        sla = 480
    entry = pharmacy._formulary_entry(session, code) if code else None
    # Phase 17 (BD-08): the order's dose context rides along so
    # weight-based (mg/kg) rules evaluate in the same check; the
    # evaluations are also surfaced explicitly (mgkg_evaluations) so
    # a cannot-evaluate status is visible, never a silent pass.
    dose_value, dose_unit = pharmacy.dose_quantity_of(body)
    blockers, warnings = (pharmacy.medication_monitoring_check(
        session, patient_ref, code, dose_value=dose_value,
        dose_unit=dose_unit,
        doses_per_day=pharmacy.doses_per_day_of(body))
        if code else ([], []))
    _, _, mgkg_evaluations = (pharmacy.weight_based_dose_check(
        session, patient_ref, code, dose_value=dose_value,
        dose_unit=dose_unit,
        doses_per_day=pharmacy.doses_per_day_of(body))
        if code else ([], [], []))
    # Phase 16 (genomics / PGx): pharmacogenomic findings ride beside
    # the monitoring blockers/warnings — same DUR pattern, rules from
    # the published terminology/pharmacogenomics config (app.genomics).
    from .genomics import evaluate_pgx
    pgx_findings = evaluate_pgx(session, patient_ref, [code]) if code else []
    # Phase 17 (BD-70): interaction findings for this order against
    # the patient's other open medication orders, checked against
    # the deployer-supplied licensed source when one is loaded;
    # empty (with the no-source status visible queue-wide) otherwise.
    interaction_findings = (interaction_findings_for_patient(
        session, patient_ref, code, exclude_order_id=row.resource_id)
        if code else [])
    allergy_conflict = (orders._allergy_conflict(
        session, patient_ref, code) if code else None)
    # Wave A (PHARM-M1): the hard-stop signals verify_order will stop
    # on, computed by the same helper so queue and gate never drift.
    hard_stop_signals = (_hard_stop_signals(
        session, patient_ref, code, entry, allergy_conflict, blockers)
        if code else [])
    return {
        "order_id": row.resource_id,
        "patient_ref": patient_ref,
        "medication_code": code,
        "medication_display": display,
        "priority": priority,
        "status": body.get("status"),
        "authored_on": body.get("authoredOn"),
        "wait_minutes": wait_minutes,
        "sla_minutes": sla,
        "sla_breached": wait_minutes is not None and wait_minutes > sla,
        "formulary_status": (entry or {}).get("formularyStatus")
        or "formulary" if entry is not None else "non-formulary",
        "restriction_note": (entry or {}).get("restrictionNote"),
        "in_formulary": entry is not None,
        "allergy_conflict": allergy_conflict,
        "monitoring_blockers": blockers,
        "monitoring_warnings": warnings,
        "mgkg_evaluations": mgkg_evaluations,
        "pgx_findings": pgx_findings,
        "interaction_findings": interaction_findings,
        "renal": _renal_view(session, patient_ref),
        # Wave A (PHARM-M1): high-alert flag + the computed hard-stop
        # signals (the verification panel renders these and collects
        # the governed override when any are present).
        "high_alert": bool(entry is not None
                           and entry.get("highAlert") is True),
        "hard_stop_signals": hard_stop_signals,
    }


@router.get("/verification-queue")
def verification_queue(session: Session = Depends(get_db),
                       user: User = Depends(require_capability("pharmacy.read"))):
    """The pharmacist verification queue: signed/active medication
    orders not yet verified, ordered by clinical priority then wait.
    Each item carries everything the verifier needs at a glance —
    formulary status + restriction, allergy conflict, monitoring
    problems, renal flag, and its SLA posture."""
    ops = _ops(session)
    verified = _verified_order_ids(session)
    items = [_queue_item(session, row, ops)
             for row in _open_med_orders(session)
             if row.resource_id not in verified]
    items.sort(key=lambda i: (PRIORITY_RANK.get(i["priority"], 3),
                              i["authored_on"] or "", i["order_id"]))
    return {"items": items, "total": len(items),
            "dur_override_reason_codes": _dur_override_codes(ops),
            "verification_validity_hours":
                _verification_validity_hours(session)}


@router.post("/verification/{order_id}/verify", status_code=201)
async def verify_order(order_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("pharmacy.verify"))):
    """Pharmacist verification of one medication order — a clinical
    sign-off. Refusals: order not in a verifiable state (409),
    already CURRENTLY verified (409), medication not in the
    published formulary / non-formulary (422), restricted without an
    explicit restriction acknowledgement (422, naming the
    restriction), a PGx "avoid" finding (422), and — Wave A
    (PHARM-M1) — a hard-stop signal (recorded-allergy conflict,
    monitoring blocker, high-alert medication) without a governed
    override or a recorded "override" DUR disposition for that
    signal (422, naming each signal). A revoked / stale / expired
    verification is NOT "already verified": re-verifying reuses the
    row and makes it current at the order's present version.
    On success the verification is recorded + audited AND the
    `pharmacy.dur.screened` event is emitted into the Phase 10 rules
    engine with the patient's creatinine clearance — the seeded
    renal-dose-review rule may open a review Task."""
    payload = await _json_body(request)
    row = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "MedicationRequest",
            FhirResource.resource_id == order_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"MedicationRequest/{order_id} was not found")
    body = row.body or {}
    if body.get("status") not in ("signed", "active"):
        raise HTTPException(
            status_code=409,
            detail=(f"MedicationRequest/{order_id} is "
                    f"'{body.get('status')}'; only a signed or active order "
                    "can be verified"))
    existing = session.scalars(select(PharmacyVerification).where(
        PharmacyVerification.order_id == order_id)).first()
    if existing is not None and verification_state(
            session, order_id, record=existing,
            order_row=row)["state"] == "current":
        raise HTTPException(
            status_code=409,
            detail=f"MedicationRequest/{order_id} is already verified")
    _, code, _ = orders._code_of(body, "medicationCodeableConcept")
    if not code:
        raise HTTPException(status_code=422,
                            detail="Order carries no medication code")
    patient_ref = row.patient_ref or (
        (body.get("subject") or {}).get("reference") or "")
    # Phase 16 (genomics / PGx): a pharmacogenomic "avoid" finding for
    # this patient + medication refuses verification, naming the
    # finding — the DUR pattern; rules are the published
    # terminology/pharmacogenomics config (app.genomics.evaluate_pgx).
    # The check runs BEFORE the formulary refusals below by design: a
    # genomic contraindication is a patient-safety stop and must be
    # reported on its own terms, never masked by a formulary status
    # (the seeded EXAMPLE-rule drugs are not on the seeded formulary).
    from .genomics import evaluate_pgx
    for finding in evaluate_pgx(session, patient_ref, [code]):
        if finding.get("action") == "avoid":
            raise HTTPException(
                status_code=422,
                detail=(f"Pharmacogenomic contraindication: "
                        f"{finding['message']} ({finding['gene']} / "
                        f"{finding['variant']} vs {finding['drug']}) — "
                        "verification refused"))
    entry = pharmacy._formulary_entry(session, code)
    status = (entry or {}).get("formularyStatus") or "formulary" \
        if entry is not None else "non-formulary"
    if status == "non-formulary":
        raise HTTPException(
            status_code=422,
            detail=(f"'{code}' is non-formulary in the published formulary; "
                    "it cannot be verified — a formulary exception must be "
                    "published in Studio before this order can proceed"))
    acknowledged = payload.get("restriction_acknowledged") is True
    if status == "restricted" and not acknowledged:
        raise HTTPException(
            status_code=422,
            detail=(f"'{code}' is formulary-restricted: "
                    f"{(entry or {}).get('restrictionNote') or 'restriction applies'}. "
                    "Verify with 'restriction_acknowledged': true to record "
                    "that the restriction was reviewed"))
    # Wave A (PHARM-M1) — hard-stop signals. A recorded allergy
    # conflict, a monitoring BLOCKER (warnings never block) or a
    # high-alert medication refuses verification 422 naming each
    # signal, UNLESS a recorded "override" DUR disposition already
    # satisfies that signal, or the payload carries a governed
    # override: override_reason_code in the published
    # `durOverrideReasonCodes` + a non-empty override_note. The
    # override (and any disposition relied on) is recorded on the
    # verification row and in the audit. Interaction findings stay
    # flag-only (BD-70), exactly as before.
    dose_value, dose_unit = pharmacy.dose_quantity_of(body)
    mon_blockers, _mon_warnings = pharmacy.medication_monitoring_check(
        session, patient_ref, code, dose_value=dose_value,
        dose_unit=dose_unit,
        doses_per_day=pharmacy.doses_per_day_of(body))
    hard_signals = _hard_stop_signals(
        session, patient_ref, code, entry,
        orders._allergy_conflict(session, patient_ref, code),
        mon_blockers)
    dispositions = _dispositions_for(session, order_id)
    satisfied_keys = {(d.signal_type, d.signal_key) for d in dispositions
                      if d.disposition == "override"}
    via_disposition = [s for s in hard_signals
                       if (s["type"], s["signal_key"]) in satisfied_keys]
    remaining = [s for s in hard_signals
                 if (s["type"], s["signal_key"]) not in satisfied_keys]
    override_code = payload.get("override_reason_code")
    override_note = payload.get("override_note")
    governed_codes = _dur_override_codes(_ops(session))
    override_valid = isinstance(override_code, str) \
        and override_code in governed_codes \
        and isinstance(override_note, str) and bool(override_note.strip())
    if remaining and not override_valid:
        named = "; ".join(f"{s['type']}: {s['detail']}" for s in remaining)
        raise HTTPException(
            status_code=422,
            detail=(f"Verification refused — hard-stop signal(s) on "
                    f"MedicationRequest/{order_id}: {named}. To verify "
                    "past a hard stop, supply a governed override: "
                    "'override_reason_code' (one of: "
                    f"{', '.join(governed_codes) if governed_codes else 'none — no codes are published in the pharmacy-ops config'}) "
                    "and a non-empty 'override_note' — or record an "
                    "'override' DUR disposition for the signal first"))
    overridden_signals = (
        [{**s, "via": "disposition"} for s in via_disposition]
        + [{**s, "via": "override"} for s in remaining]
        if hard_signals else [])
    recorded_override_code = override_code if (remaining and override_valid) \
        else None
    recorded_override_note = override_note.strip() \
        if (remaining and override_valid) else None
    crcl = _creatinine_clearance(session, patient_ref)
    # Phase 17 (BD-70): the DUR screen at verification includes the
    # interaction check against the patient's other open orders,
    # executed against the loaded licensed source when one exists.
    # Findings FLAG (severity + source attribution ride the response
    # and the audit); they do not refuse verification on their own —
    # the pharmacist disposes, like the monitoring-warning pattern.
    interaction_findings = interaction_findings_for_patient(
        session, patient_ref, code, exclude_order_id=order_id)
    interaction_status = _interaction_status(session)
    note_value = payload.get("note") if isinstance(payload.get("note"), str) \
        else None
    if existing is not None:
        # Re-verification of a revoked / stale / expired row: the
        # unique row is refreshed in place and becomes current at
        # the order's present version; the revocation is cleared
        # (its record lives in the audit chain).
        record = existing
        record.patient_ref = patient_ref
        record.medication_code = code
        record.verified_by = user.username
        record.verified_at = _now()
        record.restriction_acknowledged = acknowledged
        record.note = note_value
        record.status = "active"
        record.revoked_by = None
        record.revoked_at = None
        record.revoke_reason = None
        record.order_version = row.version
        record.override_reason_code = recorded_override_code
        record.override_note = recorded_override_note
        record.overridden_signals = json.dumps(overridden_signals) \
            if overridden_signals else None
    else:
        record = PharmacyVerification(
            order_id=order_id, patient_ref=patient_ref,
            medication_code=code, verified_by=user.username,
            verified_at=_now(), restriction_acknowledged=acknowledged,
            note=note_value, status="active", order_version=row.version,
            override_reason_code=recorded_override_code,
            override_note=recorded_override_note,
            overridden_signals=json.dumps(overridden_signals)
            if overridden_signals else None)
        session.add(record)
    session.flush()
    append_audit(session, actor=user.username, action="pharmacy.verify",
                 entity=f"MedicationRequest/{order_id}",
                 payload={"medication_code": code,
                          "formulary_status": status,
                          "restriction_acknowledged": acknowledged,
                          "crcl_ml_min": crcl,
                          "order_version": row.version,
                          "hard_stop_signals": hard_signals,
                          "overridden_signals": overridden_signals,
                          "override_reason_code": recorded_override_code,
                          "override_note": recorded_override_note,
                          "dur_disposition_ids":
                              [d.id for d in dispositions
                               if d.disposition == "override"],
                          "interaction_findings": interaction_findings,
                          "interaction_status":
                              interaction_status.get("status")})
    firings = evaluate_rules(
        session, "pharmacy.dur.screened",
        {"crcl_ml_min": crcl, "medication_code": code,
         "order_id": order_id, "patient_ref": patient_ref},
        actor=user.username)
    session.commit()
    return {"verification": {
                **_verification_out(record),
                "formulary_status": status,
                "crcl_ml_min": crcl},
            "rule_firings": firings,
            "interaction_findings": interaction_findings,
            "interaction_checking": interaction_status}


@router.post("/verification/{order_id}/revoke", status_code=201)
async def revoke_verification(order_id: str, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability("pharmacy.verify"))):
    """Revoke a recorded verification (Wave A, PHARM-B1): reason
    required (422 without). The row flips to status "revoked" with
    revoked_by / revoked_at / revoke_reason recorded and the act
    audited; the order immediately returns to the verification
    queue and can no longer be dispensed or administered against
    the revoked verification."""
    payload = await _json_body(request)
    reason = _require_str(payload, "reason")
    record = session.scalars(select(PharmacyVerification).where(
        PharmacyVerification.order_id == order_id)).first()
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No verification on file for MedicationRequest/{order_id}")
    if (getattr(record, "status", None) or "active") == "revoked":
        raise HTTPException(
            status_code=409,
            detail=(f"The verification of MedicationRequest/{order_id} "
                    "is already revoked"))
    record.status = "revoked"
    record.revoked_by = user.username
    record.revoked_at = _now()
    record.revoke_reason = reason
    session.flush()
    append_audit(session, actor=user.username,
                 action="pharmacy.verification.revoke",
                 entity=f"MedicationRequest/{order_id}",
                 payload={"medication_code": record.medication_code,
                          "verified_by": record.verified_by,
                          "revoked_by": user.username, "reason": reason})
    session.commit()
    state = verification_state(session, order_id, record=record)
    return {"verification": _verification_out(record),
            "state": state["state"], "reason": state["reason"]}


@router.get("/dur-queue")
def dur_queue(session: Session = Depends(get_db),
              user: User = Depends(require_capability("pharmacy.read"))):
    """The DUR (drug utilisation review) queue: open medication orders
    carrying at least one reviewable signal — recorded-allergy
    conflict, a medication-monitoring problem (the Phase 9 rules),
    duplicate therapy, or a renal flag. Computed from the chart, like
    the MAR. Drug-interaction signals are reported through the
    `interaction_checking` block — inert until a knowledge source is
    attached (see /pharmacy/interactions/status)."""
    verified = _verified_order_ids(session)
    open_orders = _open_med_orders(session)
    by_patient_code: dict[tuple[str, str], int] = {}
    for row in open_orders:
        _, code, _ = orders._code_of(row.body or {},
                                     "medicationCodeableConcept")
        patient_ref = row.patient_ref or ""
        if code:
            key = (patient_ref, code)
            by_patient_code[key] = by_patient_code.get(key, 0) + 1
    items = []
    for row in open_orders:
        body = row.body or {}
        _, code, display = orders._code_of(body,
                                          "medicationCodeableConcept")
        if not code:
            continue
        patient_ref = row.patient_ref or (
            (body.get("subject") or {}).get("reference") or "")
        signals: list[dict] = []
        conflict = orders._allergy_conflict(session, patient_ref, code)
        if conflict is not None:
            signals.append({
                "type": "allergy", "signal_key": str(code),
                "detail": (f"Recorded allergy to {conflict['display']} "
                           f"(AllergyIntolerance/{conflict['allergy_id']})")})
        dose_value, dose_unit = pharmacy.dose_quantity_of(body)
        blockers, warnings = pharmacy.medication_monitoring_check(
            session, patient_ref, code, dose_value=dose_value,
            dose_unit=dose_unit,
            doses_per_day=pharmacy.doses_per_day_of(body))
        for blocker in blockers:
            signals.append({
                "type": "monitoring-block",
                "signal_key": str(blocker.get("lab_code") or "weight-based"),
                "detail": blocker["problem"]})
        for warning in warnings:
            signals.append({
                "type": "monitoring-warning",
                "signal_key": str(warning.get("lab_code") or "weight-based"),
                "detail": warning["problem"]})
        duplicates = by_patient_code.get((patient_ref, code), 0) - 1
        if duplicates > 0:
            signals.append({
                "type": "duplicate-therapy", "signal_key": str(code),
                "detail": (f"{duplicates} other open order(s) for the same "
                           "medication — possible duplicate therapy")})
        renal = _renal_view(session, patient_ref)
        if renal["flagged"]:
            signals.append({
                "type": "renal", "signal_key": "crcl",
                "detail": (f"CrCl {renal['crcl_ml_min']} mL/min is below "
                           f"the {renal['threshold_ml_min']} mL/min "
                           "review threshold (rule config)")})
        # Phase 17 (BD-70): interaction signals from the loaded
        # licensed source (inert — no signals — without one).
        for finding in interaction_findings_for_patient(
                session, patient_ref, code,
                exclude_order_id=row.resource_id):
            signals.append({
                "type": "interaction",
                "signal_key": str(finding.get("checked_against") or ""),
                "severity": finding["severity"],
                "source": finding["source"],
                "detail": (f"Interaction ({finding['severity']}, "
                           f"source: {finding['source']}): "
                           f"{finding['drug_a']} + {finding['drug_b']} — "
                           f"{finding['summary'] or finding['mechanism']}")})
        if signals:
            items.append({
                "order_id": row.resource_id, "patient_ref": patient_ref,
                "medication_code": code, "medication_display": display,
                "verified": row.resource_id in verified,
                "signals": signals,
                # Wave A (PHARM-M1): the recorded dispositions ride
                # the item — the queue is actionable, not display-only.
                "dispositions": [_disposition_out(d) for d in
                                 _dispositions_for(session,
                                                   row.resource_id)]})
    return {"items": items, "total": len(items),
            "interaction_checking": _interaction_status(session),
            "dur_override_reason_codes":
                _dur_override_codes(_ops(session)),
            "dur_dispositions": list(DUR_DISPOSITIONS)}


@router.post("/dur/{order_id}/disposition", status_code=201)
async def dur_disposition(order_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("pharmacy.verify"))):
    """Record a DUR disposition for one signal on one order (Wave A,
    PHARM-M1). The disposition names the signal by the stable
    (signal_type, signal_key) pair the DUR queue emits, a disposition
    in {override, order-changed, therapy-changed, not-applicable}, a
    governed reason code (the published pharmacy-ops
    `durOverrideReasonCodes`) and a note. A recorded "override"
    disposition satisfies that signal at verification time. Audited."""
    payload = await _json_body(request)
    order = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "MedicationRequest",
            FhirResource.resource_id == order_id)).first()
    if order is None:
        raise HTTPException(status_code=404,
                            detail=f"MedicationRequest/{order_id} was not found")
    signal_type = _require_str(payload, "signal_type")
    signal_key = _require_str(payload, "signal_key")
    disposition = payload.get("disposition")
    if disposition not in DUR_DISPOSITIONS:
        raise HTTPException(
            status_code=422,
            detail=(f"'disposition' must be one of: "
                    f"{', '.join(DUR_DISPOSITIONS)}"))
    reason_code = payload.get("reason_code")
    governed_codes = _dur_override_codes(_ops(session))
    if not isinstance(reason_code, str) or reason_code not in governed_codes:
        raise HTTPException(
            status_code=422,
            detail=("'reason_code' must be one of the governed DUR "
                    "override reason codes published in the pharmacy-ops "
                    f"config: {', '.join(governed_codes) if governed_codes else 'none published — dispositions cannot be recorded until a site publishes codes'}"))
    note = _require_str(payload, "note")
    record = PharmacyDurDisposition(
        order_id=order_id, signal_type=signal_type,
        signal_key=signal_key, disposition=disposition,
        reason_code=reason_code, note=note, by=user.username, at=_now())
    session.add(record)
    session.flush()
    append_audit(session, actor=user.username,
                 action="pharmacy.dur.disposition",
                 entity=f"MedicationRequest/{order_id}",
                 payload={"signal_type": signal_type,
                          "signal_key": signal_key,
                          "disposition": disposition,
                          "reason_code": reason_code, "note": note})
    session.commit()
    return {"disposition": _disposition_out(record)}


# ---------------------------------------------------------------------------
# Drug–drug interaction checking (Phase 17, BD-70) — bring-your-own
# licensed source. The config slot `interactionKnowledgeSource` in
# the published pharmacy-ops config names a DEPLOYER-SUPPLIED source:
#
#   "interactionKnowledgeSource": {
#       "name": "<source name>",
#       "reference": "<file path, file:// URL, or http(s) URL>",
#       "format": "opencare-interactions-json-v1" | "json",
#       "licenceHolder": "<licence holder — the attestation>",
#       "licenceAttestation": "<attestation text>"        (optional)
#   }
#
# The loader REFUSES (checking stays inert, refusal stated) when the
# attestation is missing (no source name or no licence holder), the
# format is unsupported, the reference cannot be read, or the source
# content is malformed (not JSON, no pairs list, a pair missing its
# two drug codes / severity / summary-or-mechanism, an unknown
# severity). NOTHING is invented, scraped or synthesised: pairs,
# severities, mechanisms and summaries come only from the loaded
# source and are attributed to it in every finding. The loaded pair
# index is cached in-process keyed by the published config's
# fingerprint; publishing a new config version reloads.
#
# Source file shape (JSON): an object {"pairs": [...]} (or a bare
# list) whose pairs carry drugA / drugB codes (drug_a / codeA also
# accepted), optional drugADisplay / drugBDisplay, a severity in
# {contraindicated, major, moderate, minor}, and a mechanism and/or
# summary attributed to the source.
# ---------------------------------------------------------------------------

INTERACTION_FORMATS = ("opencare-interactions-json-v1", "json")
INTERACTION_SEVERITIES = ("contraindicated", "major", "moderate", "minor")

# In-process loaded-source store: {"fingerprint", "status", "index"}
# where index maps a sorted (codeA, codeB) tuple to its finding
# template. Server-side state derived ONLY from published config +
# the deployer-supplied source file; never seeded with data.
_INTERACTION_CACHE: dict = {"fingerprint": None, "status": None, "index": {}}


def _no_source_status() -> dict:
    """The shipped no-source status — byte-identical to the Phase 11
    integration point's response (pinned by tests since Phase 11)."""
    return {
        "status": "no-source", "configured": False, "source": None,
        "message": ("No drug-interaction knowledge source is "
                    "configured (terminology/pharmacy-ops "
                    "'interactionKnowledgeSource' is empty). "
                    "Interaction checking is INERT: no pair is "
                    "checked and no severity is asserted.")}


def _interaction_refusal(source, reason: str) -> dict:
    name = source.get("name") if isinstance(source, dict) else None
    return {
        "status": "source-refused", "configured": False,
        "source": name if isinstance(name, str) and name else None,
        "refusal_reason": reason,
        "message": (f"Interaction knowledge source refused: {reason}. "
                    "Interaction checking remains INERT: no pair is "
                    "checked and no severity is asserted.")}


def _read_interaction_reference(reference: str) -> str:
    """Read a deployer-supplied source reference: a file path
    (absolute, or relative to the backend directory), a file:// URL,
    or an http(s) URL. Raises ValueError naming the failure."""
    if reference.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(reference, timeout=5) as resp:
                return resp.read().decode("utf-8")
        except (urllib.error.URLError, UnicodeDecodeError, OSError) as exc:
            raise ValueError(
                f"source URL '{reference}' could not be read ({exc})")
    path_text = reference[len("file://"):] \
        if reference.startswith("file://") else reference
    path = Path(path_text)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path_text
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"source file '{reference}' could not be read ({exc})")


def _pair_codes(pair: dict) -> tuple[str | None, str | None]:
    a = pair.get("drugA") or pair.get("drug_a") or pair.get("codeA") \
        or pair.get("a")
    b = pair.get("drugB") or pair.get("drug_b") or pair.get("codeB") \
        or pair.get("b")
    a = str(a).strip() if isinstance(a, (str, int)) and str(a).strip() else None
    b = str(b).strip() if isinstance(b, (str, int)) and str(b).strip() else None
    return a, b


def _load_interaction_source(session: Session, force: bool = False):
    """Load (or return the cached load of) the configured interaction
    source. Returns (status_dict, pair_index). The no-source path
    never touches the cache or the filesystem."""
    source = _ops(session).get("interactionKnowledgeSource")
    if not source:
        return _no_source_status(), {}
    fingerprint = json.dumps(source, sort_keys=True, default=str)
    if not force and _INTERACTION_CACHE.get("fingerprint") == fingerprint \
            and _INTERACTION_CACHE.get("status") is not None:
        return _INTERACTION_CACHE["status"], _INTERACTION_CACHE["index"]

    def refuse(reason: str):
        status = _interaction_refusal(source, reason)
        _INTERACTION_CACHE.update(
            {"fingerprint": fingerprint, "status": status, "index": {}})
        return status, {}

    if not isinstance(source, dict):
        return refuse("the configured 'interactionKnowledgeSource' must "
                      "be an object carrying name, reference, format and "
                      "licenceHolder — a bare name carries no licence "
                      "attestation and cannot be loaded")
    name = source.get("name")
    if not isinstance(name, str) or not name.strip():
        return refuse("the source carries no name — the licence "
                      "attestation (source name + licence holder) is "
                      "required and is missing its name")
    holder = source.get("licenceHolder") or source.get("licence_holder") \
        or source.get("licenseHolder")
    if not isinstance(holder, str) or not holder.strip():
        return refuse(f"source '{name}' carries no licenceHolder — the "
                      "licence attestation is required: a deployer may "
                      "only load a source it is licensed to use, and "
                      "must attest the licence holder in the published "
                      "configuration")
    reference = source.get("reference") or source.get("location") \
        or source.get("uri") or source.get("path")
    if not isinstance(reference, str) or not reference.strip():
        return refuse(f"source '{name}' names no reference (file path "
                      "or URL) to load from")
    declared_format = source.get("format")
    if declared_format not in INTERACTION_FORMATS:
        return refuse(f"source '{name}' declares format "
                      f"'{declared_format}'; supported formats: "
                      f"{', '.join(INTERACTION_FORMATS)}")
    try:
        text = _read_interaction_reference(reference.strip())
    except ValueError as exc:
        return refuse(str(exc))
    try:
        document = json.loads(text)
    except ValueError:
        return refuse(f"source '{name}' content is not valid JSON")
    if isinstance(document, list):
        pairs_raw, file_format = document, None
    elif isinstance(document, dict):
        pairs_raw = document.get("pairs")
        file_format = document.get("format")
    else:
        return refuse(f"source '{name}' content must be a JSON object "
                      "with a 'pairs' list (or a bare list of pairs)")
    if file_format is not None and declared_format not in ("json",) \
            and file_format != declared_format:
        return refuse(f"source '{name}' file declares format "
                      f"'{file_format}' but the configuration declares "
                      f"'{declared_format}'")
    if not isinstance(pairs_raw, list) or not pairs_raw:
        return refuse(f"source '{name}' declares no interaction pairs")
    index: dict[tuple[str, str], dict] = {}
    for i, pair in enumerate(pairs_raw):
        if not isinstance(pair, dict):
            return refuse(f"source '{name}' pair[{i}] is not an object")
        code_a, code_b = _pair_codes(pair)
        if code_a is None or code_b is None or code_a == code_b:
            return refuse(f"source '{name}' pair[{i}] must name two "
                          "distinct drug codes (drugA / drugB)")
        severity = str(pair.get("severity") or "").strip().lower()
        if severity not in INTERACTION_SEVERITIES:
            return refuse(f"source '{name}' pair[{i}] ({code_a} + "
                          f"{code_b}) carries severity "
                          f"'{pair.get('severity')}', not one of "
                          f"{', '.join(INTERACTION_SEVERITIES)}")
        mechanism = pair.get("mechanism")
        summary = pair.get("summary") or pair.get("description")
        if not (isinstance(mechanism, str) and mechanism.strip()) \
                and not (isinstance(summary, str) and summary.strip()):
            return refuse(f"source '{name}' pair[{i}] ({code_a} + "
                          f"{code_b}) carries neither a mechanism nor "
                          "a summary attributed to the source")
        key = tuple(sorted((code_a, code_b)))
        index[key] = {
            "drug_a": key[0], "drug_b": key[1],
            "drug_a_display": pair.get("drugADisplay")
            or pair.get("drug_a_display"),
            "drug_b_display": pair.get("drugBDisplay")
            or pair.get("drug_b_display"),
            "severity": severity,
            "mechanism": mechanism.strip()
            if isinstance(mechanism, str) else None,
            "summary": summary.strip()
            if isinstance(summary, str) else None,
            "source": name.strip(), "licence_holder": holder.strip()}
    status = {
        "status": "loaded", "configured": True, "source": name.strip(),
        "licence_holder": holder.strip(), "pairs_loaded": len(index),
        "message": (f"Interaction checking is ACTIVE against the "
                    f"licensed source '{name.strip()}' (licence holder: "
                    f"{holder.strip()}); {len(index)} interaction "
                    "pair(s) loaded. Findings are attributed to this "
                    "source.")}
    _INTERACTION_CACHE.update(
        {"fingerprint": fingerprint, "status": status, "index": index})
    return status, index


def _interaction_status(session: Session) -> dict:
    """The interaction-checking status. NO knowledge source ships
    with this system (Phase 11 evaluation, SQA report): with the
    slot empty the status is the explicit no-source statement
    (byte-identical since Phase 11), never a silent empty result;
    with a deployer-supplied licensed source named, it is the
    loader's loaded / refused statement."""
    status, _ = _load_interaction_source(session)
    return status


def evaluate_interactions(session: Session, medication_codes):
    """Pair-check medication codes against the loaded source.
    Returns (status, pairs_checked, findings): pairs_checked counts
    the distinct unordered pairs actually checked (0 when inert, so
    'not checked' stays distinguishable from 'checked, none found');
    findings carry severity + mechanism/summary + source
    attribution, all from the loaded source."""
    status, index = _load_interaction_source(session)
    if status.get("status") != "loaded":
        return status, 0, []
    codes = sorted({str(c) for c in medication_codes if c})
    findings = []
    pairs_checked = 0
    for i, code_a in enumerate(codes):
        for code_b in codes[i + 1:]:
            pairs_checked += 1
            finding = index.get((code_a, code_b))
            if finding is not None:
                findings.append(dict(finding))
    return status, pairs_checked, findings


def interaction_findings_for_patient(session: Session, patient_ref: str,
                                     medication_code: str,
                                     exclude_order_id: str | None = None
                                     ) -> list[dict]:
    """Interaction findings for one candidate medication against the
    patient's OTHER open (signed/active) medication orders — the
    verification / DUR / CPOE surface helper. [] when inert."""
    if not medication_code:
        return []
    others: dict[str, str] = {}
    for row in _open_med_orders(session):
        row_patient = row.patient_ref or (
            (row.body or {}).get("subject") or {}).get("reference") or ""
        if row_patient != patient_ref:
            continue
        if exclude_order_id is not None \
                and row.resource_id == exclude_order_id:
            continue
        _, other_code, _ = orders._code_of(row.body or {},
                                           "medicationCodeableConcept")
        if other_code and other_code != medication_code:
            others.setdefault(str(other_code), row.resource_id)
    if not others:
        return []
    _, _, findings = evaluate_interactions(
        session, [medication_code, *others.keys()])
    for finding in findings:
        other = finding["drug_b"] if finding["drug_a"] == str(medication_code) \
            else finding["drug_a"]
        finding["checked_against"] = other
        finding["checked_against_order_id"] = others.get(other)
    return findings


@router.get("/interactions/status")
def interaction_status(session: Session = Depends(get_db),
                       user: User = Depends(require_capability("pharmacy.read"))):
    return _interaction_status(session)


@router.post("/interactions/load", status_code=201)
def interaction_load(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("pharmacy.verify"))):
    """(Re)load the configured interaction source NOW — the governed
    loader act. The load (or refusal) is written to the hash-chained
    audit. A refused source is a 422 naming the refusal reason; a
    successful load returns the loaded status with the pair count."""
    status, _ = _load_interaction_source(session, force=True)
    if status.get("status") == "loaded":
        append_audit(session, actor=user.username,
                     action="pharmacy.interactions.load",
                     entity="config:terminology/pharmacy-ops",
                     payload={"source": status.get("source"),
                              "licence_holder": status.get("licence_holder"),
                              "pairs_loaded": status.get("pairs_loaded")})
        session.commit()
        return status
    append_audit(session, actor=user.username,
                 action="pharmacy.interactions.load_refused",
                 entity="config:terminology/pharmacy-ops",
                 payload={"source": status.get("source"),
                          "refusal_reason": status.get("refusal_reason"),
                          "status": status.get("status")})
    session.commit()
    raise HTTPException(status_code=422, detail=status["message"])


@router.post("/interactions/check")
async def interaction_check(request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("pharmacy.read"))):
    """Pair-check a list of medication codes against the configured
    interaction knowledge source. With no source attached (the shipped
    state) this returns the no-source status, zero pairs checked and
    zero findings — an honest 'not checked', distinguishable from
    'checked, nothing found'. With a loaded licensed source, pairs
    are checked against it and findings carry the source's severity
    and attribution."""
    payload = await _json_body(request)
    codes = payload.get("medication_codes")
    if not isinstance(codes, list) \
            or any(not isinstance(c, str) or not c for c in codes):
        raise HTTPException(
            status_code=422,
            detail="'medication_codes' must be a list of medication codes")
    status, pairs_checked, findings = evaluate_interactions(session, codes)
    if status.get("status") == "loaded":
        append_audit(session, actor=user.username,
                     action="pharmacy.interactions.check",
                     entity=f"source:{status.get('source')}",
                     payload={"medication_codes": codes,
                              "pairs_checked": pairs_checked,
                              "finding_count": len(findings),
                              "severities": sorted({f["severity"]
                                                     for f in findings})})
        session.commit()
    return {**status, "medication_codes": codes,
            "pairs_checked": pairs_checked, "findings": findings}


# ---------------------------------------------------------------------------
# Controlled substances — counts, witnessed waste, discrepancy resolution
# ---------------------------------------------------------------------------

def _cs_entry(session: Session, code: str) -> dict:
    entry = pharmacy._formulary_entry(session, code)
    if entry is None or not entry.get("controlledSchedule"):
        raise HTTPException(
            status_code=422,
            detail=(f"'{code}' is not a controlled substance in the "
                    "published formulary (no controlledSchedule)"))
    return entry


def _two_person(payload: dict, user: User) -> str:
    witness = payload.get("witness")
    if not isinstance(witness, str) or not witness.strip():
        raise HTTPException(
            status_code=422,
            detail="A 'witness' is required — controlled-substance acts "
                   "are two-person acts")
    witness = witness.strip()
    if witness == user.username:
        raise HTTPException(
            status_code=422,
            detail="The witness must be a second person, not the actor")
    return witness


def _cs_count_out(row: PharmacyCsCount) -> dict:
    return {"count_id": row.count_id, "medication_code": row.medication_code,
            "location": row.location, "expected": row.expected,
            "counted": row.counted, "variance": row.variance,
            "counter": row.counter, "witness": row.witness,
            "status": row.status,
            "resolution_reason": row.resolution_reason,
            "resolved_by": row.resolved_by,
            "created_at": _aware(row.created_at).isoformat()
            if row.created_at else None}


@router.get("/cs/book")
def cs_book(session: Session = Depends(get_db),
            user: User = Depends(require_capability("pharmacy.read"))):
    """The controlled-substance count book: for every scheduled
    medication at every stock location, the expected balance (the lot
    book), the unresolved discrepancies, and whether the book
    BALANCES — it does not while any discrepancy is unresolved."""
    ops = _ops(session)
    formulary = pharmacy._formulary(session)
    items = []
    open_discrepancies = []
    for concept in formulary.get("concepts") or []:
        if not isinstance(concept, dict) \
                or not concept.get("controlledSchedule"):
            continue
        code = str(concept["code"])
        for location in _location_codes(ops):
            unresolved = session.scalars(
                select(PharmacyCsCount).where(
                    PharmacyCsCount.medication_code == code,
                    PharmacyCsCount.location == location,
                    PharmacyCsCount.status == "discrepancy")).all()
            last = session.scalars(
                select(PharmacyCsCount).where(
                    PharmacyCsCount.medication_code == code,
                    PharmacyCsCount.location == location)
                .order_by(PharmacyCsCount.id.desc())).first()
            items.append({
                "medication_code": code,
                "display": concept.get("display"),
                "schedule": concept.get("controlledSchedule"),
                "location": location,
                "expected": _lot_qty(session, code, location),
                "unresolved_discrepancies": len(unresolved),
                "balanced": len(unresolved) == 0,
                "last_count": _cs_count_out(last) if last else None,
            })
            open_discrepancies.extend(_cs_count_out(r) for r in unresolved)
    waste = session.scalars(
        select(PharmacyCsWaste).order_by(PharmacyCsWaste.id.desc())
        .limit(20)).all()
    return {"items": items, "open_discrepancies": open_discrepancies,
            "recent_waste": [{
        "waste_id": w.waste_id, "medication_code": w.medication_code,
        "location": w.location, "lot_number": w.lot_number,
        "quantity": w.quantity, "reason": w.reason,
        "wasted_by": w.wasted_by, "witness": w.witness,
        "created_at": _aware(w.created_at).isoformat() if w.created_at else None,
    } for w in waste]}


@router.post("/cs/counts", status_code=201)
async def cs_count(request: Request, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("pharmacy.cs"))):
    """Record a shift count: counted vs the expected lot-book balance,
    with a second-person witness. A variance opens a discrepancy on
    the book (see /cs/book) that must be resolved with a reason."""
    payload = await _json_body(request)
    code = _require_str(payload, "medication_code")
    location = _require_str(payload, "location")
    _cs_entry(session, code)
    if location not in _location_codes(_ops(session)):
        raise HTTPException(status_code=422,
                            detail=f"Unknown stock location '{location}'")
    counted = _require_int(payload, "counted_quantity", 0)
    witness = _two_person(payload, user)
    expected = _lot_qty(session, code, location)
    row = PharmacyCsCount(
        count_id=f"csc-{uuid.uuid4()}", medication_code=code,
        location=location, expected=expected, counted=counted,
        variance=counted - expected, counter=user.username,
        witness=witness,
        status="balanced" if counted == expected else "discrepancy")
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="pharmacy.cs.count",
                 entity=f"PharmacyCsCount/{row.count_id}",
                 payload={"medication_code": code, "location": location,
                          "expected": expected, "counted": counted,
                          "variance": row.variance, "witness": witness,
                          "discrepancy": row.status == "discrepancy"})
    session.commit()
    return _cs_count_out(row)


@router.post("/cs/counts/{count_id}/resolve", status_code=201)
async def cs_resolve(count_id: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("pharmacy.cs"))):
    """Resolve a count discrepancy with a recorded reason — only then
    does the count book for that medication+location balance again."""
    payload = await _json_body(request)
    reason = _require_str(payload, "reason")
    row = session.scalars(select(PharmacyCsCount).where(
        PharmacyCsCount.count_id == count_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"CS count '{count_id}' was not found")
    if row.status != "discrepancy":
        raise HTTPException(
            status_code=409,
            detail=f"CS count '{count_id}' is '{row.status}', not an open "
                   "discrepancy")
    row.status = "resolved"
    row.resolution_reason = reason
    row.resolved_by = user.username
    row.resolved_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="pharmacy.cs.discrepancy.resolve",
                 entity=f"PharmacyCsCount/{count_id}",
                 payload={"medication_code": row.medication_code,
                          "location": row.location,
                          "variance": row.variance, "reason": reason})
    session.commit()
    return _cs_count_out(row)


@router.post("/cs/waste", status_code=201)
async def cs_waste(request: Request, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("pharmacy.cs"))):
    """Witnessed waste of a controlled substance (a partial dose, a
    dropped tablet): two-person, reasoned, decrements the lot and the
    aggregate in the same transaction, and is audit-chained like every
    CS movement."""
    payload = await _json_body(request)
    code = _require_str(payload, "medication_code")
    location = _require_str(payload, "location")
    lot_number = _require_str(payload, "lot_number")
    reason = _require_str(payload, "reason")
    entry = _cs_entry(session, code)
    quantity = _require_int(payload, "quantity", 1)
    witness = _two_person(payload, user)
    lot = session.scalars(
        select(PharmacyLot).where(PharmacyLot.medication_code == code,
                                  PharmacyLot.location == location,
                                  PharmacyLot.lot_number == lot_number)).first()
    if lot is None:
        raise HTTPException(
            status_code=422,
            detail=f"No lot '{lot_number}' of '{code}' at '{location}'")
    if quantity > lot.quantity:
        raise HTTPException(
            status_code=409,
            detail=(f"Cannot waste {quantity} from lot '{lot_number}': "
                    f"{lot.quantity} on hand"))
    lot.quantity -= quantity
    if entry.get("stockTracked"):
        pharmacy._stock_row(session, code, entry).quantity -= quantity
    row = PharmacyCsWaste(waste_id=f"csw-{uuid.uuid4()}",
                          medication_code=code, location=location,
                          lot_number=lot_number, quantity=quantity,
                          reason=reason, wasted_by=user.username,
                          witness=witness)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="pharmacy.cs.waste",
                 entity=f"PharmacyCsWaste/{row.waste_id}",
                 payload={"medication_code": code, "location": location,
                          "lot_number": lot_number, "quantity": quantity,
                          "reason": reason, "witness": witness})
    session.commit()
    return {"waste_id": row.waste_id, "medication_code": code,
            "location": location, "lot_number": lot_number,
            "quantity": quantity, "lot_after": lot.quantity}


# ---------------------------------------------------------------------------
# Sterile compounding / IV room (USP <797> as config)
# ---------------------------------------------------------------------------

def _batch_out(batch: PharmacyCompoundBatch) -> dict:
    return {"batch_id": batch.batch_id,
            "preparation_name": batch.preparation_name,
            "category": batch.category, "storage": batch.storage,
            "compounded_at": _aware(batch.compounded_at).isoformat()
            if batch.compounded_at else None,
            "beyond_use_at": _aware(batch.beyond_use_at).isoformat()
            if batch.beyond_use_at else None,
            "ingredients": batch.ingredients or [],
            "patient_ref": batch.patient_ref, "preparer": batch.preparer,
            "verifier": batch.verifier,
            "verified_at": _aware(batch.verified_at).isoformat()
            if batch.verified_at else None,
            "status": batch.status}


def _compounding_categories(ops: dict) -> dict[str, dict]:
    compounding = ops.get("compounding") or {}
    return {str(c["code"]): c for c in compounding.get("categories") or []
            if isinstance(c, dict) and c.get("code")}


@router.get("/compounding/queue")
def compounding_queue(status: str | None = Query(default=None),
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("pharmacy.read"))):
    """The IV-room queue — deliberately separate from the verification
    queue: batches awaiting preparation/verification, with their
    computed beyond-use datetimes."""
    rows = session.scalars(
        select(PharmacyCompoundBatch).order_by(
            PharmacyCompoundBatch.id.desc())).all()
    if status:
        rows = [r for r in rows if r.status == status]
    return {"items": [_batch_out(r) for r in rows], "total": len(rows),
            "categories": _compounding_categories(_ops(session))}


@router.post("/compounding/batches", status_code=201)
async def create_compound_batch(request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(require_capability("pharmacy.compound"))):
    """Open a compounding batch record. The beyond-use datetime is
    COMPUTED here from the published category config (budHours for the
    storage condition) — a caller can neither set it nor extend it."""
    payload = await _json_body(request)
    name = _require_str(payload, "preparation_name")
    category_code = _require_str(payload, "category")
    storage = _require_str(payload, "storage")
    ops = _ops(session)
    categories = _compounding_categories(ops)
    category = categories.get(category_code)
    if category is None:
        raise HTTPException(
            status_code=422,
            detail=(f"Unknown compounding category '{category_code}'; "
                    f"published categories: {sorted(categories)}"))
    bud_hours = (category.get("budHours") or {}).get(storage)
    if not isinstance(bud_hours, (int, float)) or isinstance(bud_hours, bool):
        raise HTTPException(
            status_code=422,
            detail=(f"Category '{category_code}' has no configured "
                    f"beyond-use hours for storage '{storage}'"))
    ingredients_in = payload.get("ingredients")
    if not isinstance(ingredients_in, list) or not ingredients_in:
        raise HTTPException(status_code=422,
                            detail="'ingredients' must be a non-empty list")
    ingredients = []
    for i, item in enumerate(ingredients_in):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422,
                                detail=f"ingredients[{i}] must be an object")
        code = item.get("medication_code")
        lot_number = item.get("lot_number")
        if not isinstance(code, str) or not code.strip():
            raise HTTPException(
                status_code=422,
                detail=f"ingredients[{i}]: 'medication_code' is required")
        if not isinstance(lot_number, str) or not lot_number.strip():
            raise HTTPException(
                status_code=422,
                detail=(f"ingredients[{i}]: 'lot_number' is required — "
                        "compounding captures the lot of every ingredient"))
        entry = pharmacy._formulary_entry(session, code.strip())
        ingredients.append({
            "medication_code": code.strip(),
            "display": (entry or {}).get("display"),
            "lot_number": lot_number.strip(),
            "quantity": item.get("quantity"),
            "unit": item.get("unit"),
        })
    compounded_at = _now()
    batch = PharmacyCompoundBatch(
        batch_id=f"cpd-{uuid.uuid4()}", preparation_name=name,
        category=category_code, storage=storage,
        compounded_at=compounded_at,
        beyond_use_at=compounded_at + timedelta(hours=float(bud_hours)),
        ingredients=ingredients,
        patient_ref=payload.get("patient_ref")
        if isinstance(payload.get("patient_ref"), str) else None,
        preparer=user.username, status="in_preparation")
    session.add(batch)
    session.flush()
    append_audit(session, actor=user.username,
                 action="pharmacy.compound.create",
                 entity=f"PharmacyCompoundBatch/{batch.batch_id}",
                 payload={"preparation_name": name, "category": category_code,
                          "storage": storage, "bud_hours": bud_hours,
                          "beyond_use_at": batch.beyond_use_at.isoformat(),
                          "ingredients": ingredients})
    session.commit()
    return _batch_out(batch)


@router.post("/compounding/batches/{batch_id}/verify", status_code=201)
async def verify_compound_batch(batch_id: str,
                                session: Session = Depends(get_db),
                                user: User = Depends(require_capability("pharmacy.compound"))):
    """Release a compounded batch: a second-person check — the
    verifier must not be the preparer (422), and a batch can be
    released once (409 after)."""
    batch = session.scalars(select(PharmacyCompoundBatch).where(
        PharmacyCompoundBatch.batch_id == batch_id)).first()
    if batch is None:
        raise HTTPException(status_code=404,
                            detail=f"Compound batch '{batch_id}' was not found")
    if batch.status != "in_preparation":
        raise HTTPException(
            status_code=409,
            detail=f"Compound batch '{batch_id}' is '{batch.status}'")
    if batch.preparer == user.username:
        raise HTTPException(
            status_code=422,
            detail=("The verifier must be a second person, not the "
                    "preparer — sterile compounding release is a "
                    "two-person check"))
    batch.verifier = user.username
    batch.verified_at = _now()
    batch.status = "verified"
    session.flush()
    append_audit(session, actor=user.username,
                 action="pharmacy.compound.verify",
                 entity=f"PharmacyCompoundBatch/{batch_id}",
                 payload={"preparer": batch.preparer,
                          "verifier": user.username})
    session.commit()
    return _batch_out(batch)


# ---------------------------------------------------------------------------
# Barcode payloads + BCMA five-rights administration (Track G slice)
#
# Payload standards are DATA (pharmacy-ops config `barcodeStandards`):
#   wristband:  PREFIX | patient_id | dob(YYYYMMDD) | check-digits
#   med label:  PREFIX | dispense_id | lot (or "-") | check-digits
# Check digits are a weighted mod-97 over the payload body — a damaged
# or mistyped label fails parsing, it never silently scans as another
# patient or package. This is a local demonstration standard in the
# shape of real unit-dose/wristband payloads, not GS1 — the standard
# a site adopts is its published config.
# ---------------------------------------------------------------------------

def _barcode_standards(ops: dict) -> tuple[str, str, str]:
    std = ops.get("barcodeStandards") or {}
    return (std.get("wristbandPrefix") or "OCWB1",
            std.get("medLabelPrefix") or "OCML1",
            std.get("separator") or "|")


def _check_digits(body: str) -> str:
    return f"{sum((i + 1) * ord(c) for i, c in enumerate(body)) % 97:02d}"


def _make_payload(ops: dict, prefix: str, field1: str, field2: str) -> str:
    _, _, sep = _barcode_standards(ops)
    body = sep.join([prefix, field1, field2])
    return f"{body}{sep}{_check_digits(body)}"


def _parse_payload(raw: str, ops: dict) -> dict:
    wristband_prefix, med_prefix, sep = _barcode_standards(ops)
    parts = raw.strip().split(sep)
    if len(parts) != 4:
        raise ValueError(
            f"expected 4 '{sep}'-separated fields (prefix, id, check "
            f"field, check digits), got {len(parts)}")
    prefix, field1, field2, digits = parts
    if _check_digits(sep.join(parts[:3])) != digits:
        raise ValueError(
            "check digits do not match — the label is damaged, mistyped, "
            "or not an OpenCare label")
    if prefix == wristband_prefix:
        return {"type": "wristband", "patient_id": field1, "dob": field2}
    if prefix == med_prefix:
        return {"type": "med-label", "dispense_id": field1, "lot": field2}
    raise ValueError(f"unknown label prefix '{prefix}'")


def _dispense_lot(dispense_body: dict) -> str | None:
    for ext in dispense_body.get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == pharmacy.LOT_EXTENSION:
            return ext.get("valueString")
    return None


@router.get("/patients/{patient_id}/dispenses")
def patient_dispenses(patient_id: str,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("pharmacy.read"))):
    """One patient's dispense history, newest first — the feed the MAR
    scan simulator (and any dispense audit view) picks a unit dose
    from. The lot each dispense was allocated from rides along."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "MedicationDispense",
            FhirResource.patient_ref == f"Patient/{patient_id}")
        .order_by(FhirResource.id.desc()).limit(100)).all()
    items = []
    for row in rows:
        body = row.body or {}
        _, code, display = orders._code_of(body, "medicationCodeableConcept")
        prescriptions = body.get("authorizingPrescription") or []
        items.append({
            "dispense_id": row.resource_id,
            "status": body.get("status"),
            "medication_code": code,
            "medication_display": display,
            "lot_number": _dispense_lot(body),
            "quantity": (body.get("quantity") or {}).get("value"),
            "when_handed_over": body.get("whenHandedOver"),
            "request_ref": (prescriptions[0] or {}).get("reference")
            if prescriptions else None,
        })
    return {"items": items, "total": len(items)}


@router.post("/barcodes/wristband")
async def barcode_wristband(request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("pharmacy.read"))):
    """Generate a patient wristband payload: patient id + the DOB
    check field read from the Patient record itself."""
    payload = await _json_body(request)
    patient_id = _require_str(payload, "patient_id")
    patient = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Patient",
            FhirResource.resource_id == patient_id)).first()
    if patient is None:
        raise HTTPException(status_code=404,
                            detail=f"Patient/{patient_id} was not found")
    birth_date = (patient.body or {}).get("birthDate")
    dob_digits = str(birth_date).replace("-", "") if birth_date else ""
    if len(dob_digits) != 8 or not dob_digits.isdigit():
        raise HTTPException(
            status_code=422,
            detail=("Patient has no usable birthDate — the wristband "
                    "payload's DOB check field cannot be generated"))
    ops = _ops(session)
    wristband_prefix, _, _ = _barcode_standards(ops)
    return {"payload": _make_payload(ops, wristband_prefix, patient_id,
                                     dob_digits),
            "patient_id": patient_id, "dob": birth_date}


@router.post("/barcodes/med-label")
async def barcode_med_label(request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("pharmacy.read"))):
    """Generate a unit-dose medication label payload for one dispense:
    dispense id + the lot the dispense was allocated from."""
    payload = await _json_body(request)
    dispense_id = _require_str(payload, "dispense_id")
    dispense = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "MedicationDispense",
            FhirResource.resource_id == dispense_id)).first()
    if dispense is None:
        raise HTTPException(
            status_code=404,
            detail=f"MedicationDispense/{dispense_id} was not found")
    lot = _dispense_lot(dispense.body or {}) or "-"
    ops = _ops(session)
    _, med_prefix, _ = _barcode_standards(ops)
    return {"payload": _make_payload(ops, med_prefix, dispense_id, lot),
            "dispense_id": dispense_id, "lot": lot}


@router.post("/barcodes/parse")
async def barcode_parse(request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("pharmacy.read"))):
    """Parse + validate any OpenCare barcode payload (format and check
    digits); a malformed payload is a 422 naming the problem."""
    payload = await _json_body(request)
    raw = _require_str(payload, "payload")
    try:
        return _parse_payload(raw, _ops(session))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _dose_of(body: dict) -> tuple[float | None, str | None]:
    instructions = body.get("dosageInstruction") or []
    if instructions and isinstance(instructions[0], dict):
        ins = instructions[0]
        quantity = ins.get("doseQuantity")
        if not isinstance(quantity, dict):
            rates = ins.get("doseAndRate") or []
            if rates and isinstance(rates[0], dict):
                quantity = rates[0].get("doseQuantity")
        if isinstance(quantity, dict):
            value = quantity.get("value")
            if isinstance(value, (int, float)) \
                    and not isinstance(value, bool):
                return float(value), quantity.get("unit") \
                    or quantity.get("code")
    return None, None


def _route_of(body: dict) -> str | None:
    instructions = body.get("dosageInstruction") or []
    if instructions and isinstance(instructions[0], dict):
        route = instructions[0].get("route") or {}
        codings = route.get("coding") or []
        if codings and isinstance(codings[0], dict) \
                and codings[0].get("code"):
            return str(codings[0]["code"])
        if route.get("text"):
            return str(route["text"])
    return None


def _time_check(session: Session, order_row: FhirResource) -> tuple[bool, str]:
    """Right time: the scan moment must fall inside an open MAR dose
    window for the order (anchor + interval from the order, window
    width from the published order-type config — the same maths the
    MAR due list renders)."""
    now = _now()
    interval = timedelta(hours=orders._order_interval_hours(session,
                                                            order_row))
    window_hours = orders._window_hours(session)
    half = timedelta(hours=window_hours / 2.0)
    anchor = pharmacy._parse_dt((order_row.body or {}).get("authoredOn")) \
        or _aware(order_row.created_at)
    if anchor is None or interval.total_seconds() <= 0:
        return False, "the order has no dosing schedule to check against"
    k = int((now - anchor).total_seconds() // interval.total_seconds())
    for cand in (k - 1, k, k + 1):
        if cand < 0:
            continue
        due = anchor + cand * interval
        if due - half <= now <= due + half:
            return True, (f"dose due {due.isoformat()} is inside its "
                          f"±{window_hours / 2:g}h administration window")
    return False, (f"no open dose window at scan time (doses every "
                   f"{interval.total_seconds() / 3600:g}h, window "
                   f"±{window_hours / 2:g}h)")


@router.post("/bcma/administer", status_code=201)
async def bcma_administer(request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("pharmacy.administer"))):
    """Full BCMA scan administration: wristband payload + medication
    label payload in, five-rights check, MedicationAdministration out.

    Identity rights are absolute hard stops (never overridable):
    right patient (scanned wristband == the dispense's patient),
    right dispense (the scanned package's lot == the lot the dispense
    was allocated from — the wrong package does not get administered),
    right medication (the dispensed drug == the ordered drug).
    Dose / route / time failures are hard stops that require an
    `override_reason` to proceed; the override, its reason and the
    failed rights are recorded on the administration AND in the audit
    chain. Refused scans are audited as `pharmacy.bcma.scan_refused`.
    """
    payload = await _json_body(request)
    wristband_raw = _require_str(payload, "wristband_payload")
    label_raw = _require_str(payload, "med_label_payload")
    override_reason = payload.get("override_reason")
    override_reason = override_reason.strip() \
        if isinstance(override_reason, str) and override_reason.strip() \
        else None
    ops = _ops(session)

    def refuse(status: int, detail: str, context: dict | None = None):
        append_audit(session, actor=user.username,
                     action="pharmacy.bcma.scan_refused", entity="bcma-scan",
                     payload={"detail": detail, **(context or {})})
        session.commit()
        raise HTTPException(status_code=status, detail=detail)

    try:
        wristband = _parse_payload(wristband_raw, ops)
        label = _parse_payload(label_raw, ops)
    except ValueError as exc:
        refuse(422, f"Barcode scan refused: {exc}")
    if wristband["type"] != "wristband":
        refuse(422, "The first scan must be a patient wristband payload")
    if label["type"] != "med-label":
        refuse(422, "The second scan must be a medication label payload")
    dispense_id = label["dispense_id"]
    dispense = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "MedicationDispense",
            FhirResource.resource_id == dispense_id)).first()
    if dispense is None:
        refuse(422, (f"Right dispense failed: MedicationDispense/"
                     f"{dispense_id} was not found"),
               {"dispense_id": dispense_id})
    if dispense_id in pharmacy._administered_dispense_ids(session):
        refuse(409, (f"MedicationDispense/{dispense_id} has already been "
                     "administered; a dispense is administered once"),
               {"dispense_id": dispense_id})
    dispense_body = dispense.body or {}
    # Right patient — absolute.
    expected_patient = fhir._normalize_patient_ref(wristband["patient_id"])
    actual_patient = (dispense_body.get("subject") or {}).get("reference")
    if actual_patient != expected_patient:
        refuse(422, (f"Right patient failed: wristband "
                     f"'{expected_patient}' does not match the dispense's "
                     f"patient '{actual_patient}'"),
               {"dispense_id": dispense_id})
    # Right package — the scanned lot must be the dispensed lot.
    dispense_lot = _dispense_lot(dispense_body)
    if (label["lot"] or "-") != (dispense_lot or "-"):
        refuse(422, (f"Right dispense failed: the scanned package is lot "
                     f"'{label['lot']}' but dispense {dispense_id} was "
                     f"allocated lot '{dispense_lot or '-'}' — wrong "
                     "package; return it to pharmacy"),
               {"dispense_id": dispense_id})
    checks: list[dict] = [
        {"right": "right-patient", "result": "pass",
         "detail": f"wristband {expected_patient} matches the dispense"},
        {"right": "right-dispense", "result": "pass",
         "detail": f"dispense {dispense_id}, lot {dispense_lot or '-'} "
                   "matches the scanned label"},
    ]
    # Right medication — the dispensed drug must be the ordered drug.
    prescriptions = dispense_body.get("authorizingPrescription") or []
    order_row = None
    if prescriptions:
        reference = (prescriptions[0] or {}).get("reference") or ""
        if reference.startswith("MedicationRequest/"):
            order_row = session.scalars(
                select(FhirResource).where(
                    FhirResource.resource_type == "MedicationRequest",
                    FhirResource.resource_id == reference.split("/", 1)[1])
            ).first()
    if order_row is None:
        refuse(422, ("Right medication failed: dispense "
                     f"{dispense_id} has no authorizing order to verify "
                     "against"), {"dispense_id": dispense_id})
    # Wave A (PHARM-M2 + PHARM-B1): the authorizing order must carry
    # a CURRENT pharmacist verification at administration time — the
    # same single-source semantics as the dispense gate. A revoked,
    # stale or expired verification refuses here (409, audited as a
    # refused scan) even though the dispense itself was lawful when
    # it happened.
    vstate = verification_state(session, order_row.resource_id)
    if vstate["state"] != "current":
        refuse(409, (f"Verification failed: the authorizing order "
                     f"MedicationRequest/{order_row.resource_id} has no "
                     f"current pharmacist verification — verification "
                     f"state: {vstate['state']} ({vstate['reason']}). "
                     "Re-verify the order before administering"),
               {"dispense_id": dispense_id,
                "verification_state": vstate["state"]})
    order_body = order_row.body or {}
    _, dispense_code, concept = pharmacy._medication_of(dispense_body)
    _, order_code, _ = orders._code_of(order_body,
                                       "medicationCodeableConcept")
    if order_code != dispense_code:
        refuse(422, (f"Right medication failed: dispensed medication "
                     f"'{dispense_code}' does not match the ordered "
                     f"medication '{order_code}'"),
               {"dispense_id": dispense_id})
    checks.append({"right": "right-medication", "result": "pass",
                   "detail": f"{dispense_code} matches the order"})
    # Right dose — what was dispensed must be the ordered dose.
    dose_value, dose_unit = _dose_of(order_body)
    dispensed_qty = (dispense_body.get("quantity") or {}).get("value")
    if dose_value is None:
        checks.append({"right": "right-dose", "result": "fail",
                       "detail": "no structured dose (doseQuantity) is "
                                 "documented on the order"})
    elif dispensed_qty is None or float(dispensed_qty) != dose_value:
        checks.append({"right": "right-dose", "result": "fail",
                       "detail": (f"dispensed quantity {dispensed_qty} "
                                  f"does not match the ordered dose "
                                  f"{dose_value:g} {dose_unit or ''}".strip())})
    else:
        checks.append({"right": "right-dose", "result": "pass",
                       "detail": (f"dispensed {dispensed_qty} matches the "
                                  f"ordered dose {dose_value:g} "
                                  f"{dose_unit or ''}".strip())})
    # Right route — the handed-over route must be the ordered route.
    order_route = _route_of(order_body)
    dispense_route = _route_of(dispense_body)
    if not order_route:
        checks.append({"right": "right-route", "result": "fail",
                       "detail": "no route is documented on the order"})
    elif dispense_route != order_route:
        checks.append({"right": "right-route", "result": "fail",
                       "detail": (f"dispensed route '{dispense_route}' "
                                  f"does not match the ordered route "
                                  f"'{order_route}'")})
    else:
        checks.append({"right": "right-route", "result": "pass",
                       "detail": f"route {order_route} matches the order"})
    # Right time — an open MAR dose window at scan time.
    on_time, time_detail = _time_check(session, order_row)
    checks.append({"right": "right-time",
                   "result": "pass" if on_time else "fail",
                   "detail": time_detail})
    failed = [c for c in checks if c["result"] == "fail"]
    if failed and not override_reason:
        refuse(422, ("BCMA hard stop: " + "; ".join(
            f"{c['right']} — {c['detail']}" for c in failed) +
            ". To proceed, supply 'override_reason' — the override and "
            "its reason are recorded on the administration and in the "
            "audit chain"),
            {"dispense_id": dispense_id,
             "failed_rights": [c["right"] for c in failed]})
    admin_body = {
        "resourceType": "MedicationAdministration",
        "id": str(uuid.uuid4()),
        "status": "completed",
        "medicationCodeableConcept": concept,
        "subject": dispense_body.get("subject"),
        "effectiveDateTime": _now().isoformat(),
        "performer": [{"actor": {"display": user.username}}],
        "extension": [{"url": pharmacy.DISPENSE_EXTENSION,
                       "valueReference": {
                           "reference": f"MedicationDispense/{dispense_id}"}}],
    }
    if failed:
        admin_body["extension"].append(
            {"url": BCMA_OVERRIDE_EXTENSION, "valueString": override_reason})
    if prescriptions:
        admin_body["request"] = prescriptions[0]
    if dispense_body.get("encounter"):
        admin_body["encounter"] = dispense_body["encounter"]
    rendered = fhir.create_resource("MedicationAdministration", admin_body,
                                    session, user)
    override = {"reason": override_reason,
                "rights": [c["right"] for c in failed]} if failed else None
    append_audit(session, actor=user.username,
                 action="pharmacy.bcma.administer",
                 entity=f"MedicationAdministration/{rendered['id']}",
                 payload={"dispense_id": dispense_id,
                          "medication_code": dispense_code,
                          "patient": actual_patient, "lot": dispense_lot,
                          "checks": checks, "override": override})
    session.commit()
    return {"administration": rendered,
            "bcma": {"checks": checks, "override": override}}
