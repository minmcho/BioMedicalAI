"""Phase 4A — Pharmacy: formulary, dispensing with stock control, and
BCMA-lite barcode medication administration.

Built ON the Studio customization engine like the other modules:

- the formulary is terminology config, key ``formulary`` (RxNorm-coded
  concepts carrying a ``stockTracked`` flag and an ``initialStock``),
  seeded published v1 at startup by :func:`ensure_pharmacy_defaults`;
- stock itself is server-side state (app.models.PharmacyStock), seeded
  once from the formulary's ``initialStock`` for stock-tracked meds and
  decremented in the same transaction as each dispense. Dispensing more
  than the on-hand quantity is refused with 409 BEFORE any write — stock
  can never go negative and a refused dispense changes nothing.

Flow:
- ``POST /pharmacy/dispense`` takes an ACTIVE MedicationRequest, creates
  a completed MedicationDispense linked back to it
  (``authorizingPrescription``), decrements stock when the medication is
  stock-tracked, and moves the request to status ``dispensed``.
- Administration is NOT served from this module. Wave A (PHARM-M2)
  retired the BCMA-lite ``POST /pharmacy/administer`` (typed IDs,
  three rights, a formulary-configurable subset): a parallel weak
  path beside the real gate is exactly the bypass class Wave A
  closes. The ONE administration engine is the five-rights BCMA scan
  in app.pharmacy_ops (``POST /pharmacy/bcma/administer``), which
  also requires a CURRENT pharmacist verification of the authorizing
  order (Wave A, PHARM-B1 — see app.pharmacy_ops.current_verification).

Wave A (PHARM-B1) also gates dispensing itself: ``POST
/pharmacy/dispense`` refuses 409 (audited
``pharmacy.dispense.refused_unverified``, zero state change) unless
the order carries a CURRENT verification — see
app.pharmacy_ops.verification_state for the semantics.

Phase 11 adds, in app.pharmacy_ops: lot-level inventory under this
module's aggregate stock counter, procurement with DSCSA receiving,
controlled substances, sterile compounding, the pharmacist
verification + DUR work queues, and the full five-rights BCMA scan.
Dispensing from here now allocates stock FEFO across the lots at the
configured dispense location (app.models.PharmacyLot) and stamps the
dispense with the lot (and any received serials), writing the matching
DSCSA ledger entry in the same transaction.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .configstore import published_config
from .models import (ConfigStatus, DscsaLedgerEntry, FhirResource,
                     PharmacyLot, PharmacyStock, StudioConfig, User)
from .security import get_db, require_capability

router = APIRouter(prefix="/pharmacy", tags=["pharmacy"])

FORMULARY_KEY = "formulary"
MONITORING_KEY = "medication-monitoring"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
DISPENSE_EXTENSION = "urn:opencare:extension/dispense"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def default_formulary_payload() -> dict:
    """Synthetic formulary: the RxNorm medications the seed data actually
    prescribes. Albuterol is deliberately NOT stock-tracked (a ward-stock
    inhaler) to exercise the flag; the rest carry demo stock levels.

    Phase 11: concepts also carry `formularyStatus` (formulary /
    restricted / non-formulary, with a `restrictionNote` for restricted
    items — the app.pharmacy_ops verification queue consumes it) and,
    for controlled substances, a DEA `controlledSchedule`. A concept
    published BEFORE Phase 11 carries neither field; readers treat a
    missing status as "formulary" and a missing schedule as not
    controlled, so an existing deployment's published v1 keeps working
    until the site publishes a new version."""
    def med(code, display, tracked, initial=None, status="formulary",
            restriction=None, schedule=None, high_alert=False):
        concept = {"code": code, "display": display, "active": True,
                   "system": RXNORM, "stockTracked": tracked,
                   "formularyStatus": status}
        if initial is not None:
            concept["initialStock"] = initial
        if restriction is not None:
            concept["restrictionNote"] = restriction
        if schedule is not None:
            concept["controlledSchedule"] = schedule
        if high_alert:
            # Wave A (PHARM-M1): ISMP-class high-alert flag — the
            # verification hard-stop consumes `highAlert: true` from
            # whatever formulary version a site publishes.
            concept["highAlert"] = True
        return concept

    return {
        "name": "OpenCare formulary (RxNorm, synthetic)",
        "system": RXNORM,
        # BCMA enforced rights, in enforcement order. Wave A
        # (PHARM-M2) retired the Phase 4A BCMA-lite endpoint that
        # enforced a configurable subset of these; the one surviving
        # engine (app.pharmacy_ops /pharmacy/bcma/administer) always
        # enforces all five clinical rights (patient, medication,
        # dose, route, time) plus right-dispense. The list remains
        # published reference data for the scan surfaces.
        "bcmaRights": ["right-dispense", "right-medication", "right-patient",
                       "right-dose", "right-route", "right-time"],
        "concepts": [
            med("1049221", "Lisinopril 10 MG Oral Tablet", True, 120),
            med("847232", "Metformin hydrochloride 500 MG Oral Tablet", True, 200),
            med("1044910", "Atorvastatin 20 MG Oral Tablet", True, 90),
            med("1048467", "Sertraline 50 MG Oral Tablet", True, 60),
            med("1351886", "Apixaban 5 MG Oral Tablet", True, 45,
                status="restricted",
                restriction=("Restricted — anticoagulation stewardship: "
                             "confirm indication and renal function before "
                             "verification."),
                high_alert=True),  # anticoagulant — ISMP high-alert class
            med("245314", "Albuterol 0.09 MG/ACTUAT Metered Dose Inhaler", False),
            # Synthetic controlled substances (DEA schedules are the
            # Phase 11 CS module's config input: counts, witnessed
            # waste and the perpetual book apply to these concepts).
            med("1049625", "Oxycodone hydrochloride 5 MG Oral Tablet",
                True, 40, schedule="II",
                high_alert=True),  # opioid — ISMP high-alert class
            med("197589", "Lorazepam 1 MG Oral Tablet", True, 60,
                schedule="IV"),
        ],
    }


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    existing = session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind,
                                   StudioConfig.key == key)).first()
    if existing is not None:
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


def ensure_pharmacy_defaults(session: Session) -> None:
    """Seed the formulary as published Studio config v1, and seed stock
    rows from it (idempotent: an existing stock row is the truth and is
    never reset by a restart)."""
    _seed_published(session, "terminology", FORMULARY_KEY,
                    default_formulary_payload())
    formulary = _formulary(session)
    for concept in formulary.get("concepts") or []:
        if not isinstance(concept, dict) or not concept.get("stockTracked"):
            continue
        code = concept.get("code")
        if not code:
            continue
        existing = session.scalars(
            select(PharmacyStock).where(PharmacyStock.medication_code == code)).first()
        if existing is None:
            initial = concept.get("initialStock")
            quantity = int(initial) if isinstance(initial, (int, float)) \
                and not isinstance(initial, bool) else 0
            session.add(PharmacyStock(medication_code=code, quantity=quantity))
    session.commit()


# ---------------------------------------------------------------------------
# Formulary + stock accessors
# ---------------------------------------------------------------------------

def _formulary(session: Session) -> dict:
    cfg = published_config(session, "terminology", FORMULARY_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published formulary (terminology/{FORMULARY_KEY}); "
                    "publish one in Studio before using the pharmacy"))
    return cfg.payload or {}


def _formulary_entry(session: Session, code: str) -> dict | None:
    for concept in _formulary(session).get("concepts") or []:
        if isinstance(concept, dict) and concept.get("code") == code:
            return concept
    return None


def _stock_row(session: Session, code: str, concept: dict) -> PharmacyStock:
    row = session.scalars(
        select(PharmacyStock).where(PharmacyStock.medication_code == code)).first()
    if row is None:
        initial = concept.get("initialStock")
        row = PharmacyStock(
            medication_code=code,
            quantity=int(initial) if isinstance(initial, (int, float))
            and not isinstance(initial, bool) else 0)
        session.add(row)
        session.flush()
    return row


# ---------------------------------------------------------------------------
# Lot allocation + DSCSA serial consumption (Phase 11)
#
# PharmacyLot rows (app.pharmacy_ops owns the endpoints; the seeding and
# every stock mutation keep them in step with the aggregate counter)
# are the detail under PharmacyStock. Dispensing allocates FEFO —
# first-expiry, first-out — across the lots at the configured dispense
# location, stamps the dispense with the allocated lot (and the serials
# it consumed from the receiving ledger), and writes the dispense's own
# DSCSA ledger entry. When a medication has NO lot inventory (an
# aggregate-only balance, e.g. stock that predates Phase 11 at a
# location with no lots), allocation returns nothing and dispensing
# behaves exactly as Phase 4A — the aggregate remains the truth.
# ---------------------------------------------------------------------------

OPS_KEY = "pharmacy-ops"
LOT_EXTENSION = "urn:opencare:extension/dispense-lot"
SERIAL_EXTENSION = "urn:opencare:extension/dispense-serial"


def _ops_payload(session: Session) -> dict:
    cfg = published_config(session, "terminology", OPS_KEY)
    return (cfg.payload or {}) if cfg is not None else {}


def _dispense_location(session: Session) -> str:
    loc = _ops_payload(session).get("defaultDispenseLocation")
    return loc if isinstance(loc, str) and loc else "main-pharmacy"


def _consume_serials(session: Session, code: str, lot_number: str,
                     count: int) -> list[str]:
    """Serials received for (code, lot) not yet consumed by an earlier
    dispense, oldest receipt first. Serials are optional DSCSA data:
    a receiving line that captured none simply yields none here."""
    received: list[str] = []
    rows = session.scalars(
        select(DscsaLedgerEntry).where(
            DscsaLedgerEntry.event == "receiving",
            DscsaLedgerEntry.medication_code == code,
            DscsaLedgerEntry.lot_number == lot_number)
        .order_by(DscsaLedgerEntry.id.asc())).all()
    for row in rows:
        serials = (row.detail or {}).get("serials")
        if isinstance(serials, list):
            received.extend(str(s) for s in serials)
    used: set[str] = set()
    rows = session.scalars(
        select(DscsaLedgerEntry).where(
            DscsaLedgerEntry.event == "dispense",
            DscsaLedgerEntry.medication_code == code)
        .order_by(DscsaLedgerEntry.id.asc())).all()
    for row in rows:
        serials = (row.detail or {}).get("serials")
        if isinstance(serials, list):
            used.update(str(s) for s in serials)
    return [s for s in received if s not in used][:count]


def _allocate_lots(session: Session, code: str, quantity: int,
                   location: str):
    """FEFO-allocate `quantity` of `code` across the lots at `location`.
    Returns (primary_lot, serials, taken) where taken is
    [(lot_number, qty), ...]; (None, [], []) when the medication has no
    lot inventory covering the quantity at that location."""
    lots = session.scalars(
        select(PharmacyLot).where(
            PharmacyLot.medication_code == code,
            PharmacyLot.location == location,
            PharmacyLot.quantity > 0)
        .order_by(PharmacyLot.expiry_date.asc(), PharmacyLot.id.asc())).all()
    if not lots or sum(l.quantity for l in lots) < quantity:
        return None, [], []
    remaining = quantity
    taken: list[tuple[str, int]] = []
    for lot in lots:
        if remaining <= 0:
            break
        take = min(lot.quantity, remaining)
        lot.quantity -= take
        remaining -= take
        taken.append((lot.lot_number, take))
    session.flush()
    primary = taken[0][0]
    return primary, _consume_serials(session, code, primary, quantity), taken


def _stock_out(session: Session) -> list[dict]:
    rows = {r.medication_code: r.quantity for r in session.scalars(
        select(PharmacyStock)).all()}
    out = []
    for concept in _formulary(session).get("concepts") or []:
        if not isinstance(concept, dict) or not concept.get("code"):
            continue
        tracked = bool(concept.get("stockTracked"))
        out.append({
            "code": concept["code"],
            "display": concept.get("display"),
            "system": concept.get("system") or RXNORM,
            "active": concept.get("active") is not False,
            "stock_tracked": tracked,
            "quantity": rows.get(concept["code"]) if tracked else None,
            # Phase 11 (app.pharmacy_ops verification queue + CS module):
            "formulary_status": concept.get("formularyStatus") or "formulary",
            "restriction_note": concept.get("restrictionNote"),
            "controlled_schedule": concept.get("controlledSchedule"),
        })
    return out


def _medication_of(body: dict) -> tuple[str | None, str | None, dict | None]:
    """(system, code, CodeableConcept) from medicationCodeableConcept."""
    concept = body.get("medicationCodeableConcept") or {}
    coding = concept.get("coding") or []
    if coding and isinstance(coding[0], dict):
        return coding[0].get("system"), coding[0].get("code"), concept
    return None, None, concept if isinstance(concept, dict) else None


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


# ---------------------------------------------------------------------------
# Medication monitoring gate (Phase 9) — the Phase 2 QC-gate pattern
# applied to high-risk medications: a published terminology config
# (key `medication-monitoring`) declares, per medication code, the
# monitoring labs that must be on file, current and in range before
# the medication may be dispensed (the clozapine/ANC pattern from
# roadmap §4 psychiatry). The EVALUATION is generic code; the RULES
# are configuration. With no published rules config — the default —
# the gate is inert and dispensing behaves exactly as before.
#
# Rule shape:
#   {"rules": [{"medicationCode": ..., "medicationDisplay": ...,
#               "action": "block" | "flag",          (default "block")
#               "requiredLabs": [{"code": <LOINC>, "display": ...,
#                                  "minValue": n?, "maxValue": n?,
#                                  "maxAgeDays": n?}]}]}
# "block" refuses the dispense with 409 (nothing written); "flag"
# lets it proceed and returns the problems as `monitoring_warnings`.
# Only RELEASED results count: lab drafts carrying the Phase 2
# unreleased-visibility tag are invisible here, exactly as they are
# to every other consumer.
#
# Phase 17 (BD-08 residual) — weight-based (mg/kg) rule type. A rule
# may ALSO carry a `weightBasedDosing` object (the vocabulary
# extension; the rule CONTENT stays governed config, only the
# vocabulary is code):
#   "weightBasedDosing": {
#       "doseUnit": "mg",                      (default "mg")
#       "minMgPerKgPerDose": n?, "maxMgPerKgPerDose": n?,
#       "minMgPerKgPerDay": n?,  "maxMgPerKgPerDay": n?,
#       "ageBands": [{"ageBand": "1-4y", "minAgeYears": 1,
#                     "maxAgeYears": 5, <same bound fields>}, ...]?}
# With `ageBands`, the band containing the patient's age (from the
# Patient birthDate) supplies the bounds; a patient outside every
# band is NOT-APPLICABLE (the rule does not govern that age). The
# dose is the order's structured doseQuantity (mg); the per-day
# figure multiplies by the order timing's doses/day (an order with
# no timing is assumed once-daily, and the evaluation says so). The
# weight is the patient's latest RELEASED body-weight Observation
# (LOINC 29463-7, kg). A rule that cannot be evaluated — no recorded
# weight, no birthDate for a banded rule, no structured dose, a
# non-mg dose unit — yields an explicit CANNOT-EVALUATE problem that
# blocks/flags exactly like a violation: it is never a silent pass.
# ---------------------------------------------------------------------------

def _monitoring_rules(session: Session) -> list[dict]:
    cfg = published_config(session, "terminology", MONITORING_KEY)
    if cfg is None:
        return []
    rules = (cfg.payload or {}).get("rules")
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict) and r.get("medicationCode")]


def _parse_dt(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _latest_result(session: Session, patient_ref: str,
                   code: str) -> tuple[float, datetime] | None:
    """(value, effective datetime) of the patient's LATEST released
    Observation carrying `code`, or None. Unreleased lab drafts
    (Phase 2 visibility tag) never count as monitoring evidence."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Observation",
            FhirResource.patient_ref == patient_ref)).all()
    best: tuple[float, datetime] | None = None
    for row in rows:
        body = row.body or {}
        if fhir.has_unreleased_tag(body):
            continue
        concept = body.get("code") or {}
        codes = [c.get("code") for c in concept.get("coding") or []
                 if isinstance(c, dict)]
        if code not in codes:
            continue
        quantity = body.get("valueQuantity") or {}
        value = quantity.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        effective = _parse_dt(body.get("effectiveDateTime")) \
            or _parse_dt(body.get("issued"))
        if effective is None:
            created = row.created_at
            effective = created if created.tzinfo is not None \
                else created.replace(tzinfo=timezone.utc)
        if best is None or effective > best[1]:
            best = (float(value), effective)
    return best


def _monitoring_problem(session: Session, patient_ref: str,
                        lab: dict) -> str | None:
    """Why one required monitoring lab fails for this patient, or
    None when it is on file, current and in range."""
    code = str(lab.get("code"))
    display = lab.get("display") or code
    latest = _latest_result(session, patient_ref, code)
    if latest is None:
        return f"no {display} ({code}) result on file"
    value, effective = latest
    max_age = lab.get("maxAgeDays")
    if isinstance(max_age, (int, float)) and not isinstance(max_age, bool) \
            and max_age > 0:
        age_days = (datetime.now(timezone.utc) - effective).total_seconds() / 86400.0
        if age_days > float(max_age):
            return (f"latest {display} ({code}) is {int(age_days)} days old "
                    f"(must be within {max_age} days)")
    min_value = lab.get("minValue")
    if isinstance(min_value, (int, float)) and not isinstance(min_value, bool) \
            and value < float(min_value):
        return (f"latest {display} ({code}) is {value}, below the required "
                f"minimum {min_value}")
    max_value = lab.get("maxValue")
    if isinstance(max_value, (int, float)) and not isinstance(max_value, bool) \
            and value > float(max_value):
        return (f"latest {display} ({code}) is {value}, above the allowed "
                f"maximum {max_value}")
    return None


WEIGHT_LOINC = "29463-7"      # Body weight
_MG_UNITS = ("mg", "milligram", "milligrams")
_KG_UNITS = ("kg", "kilogram", "kilograms")
_TIMING_UNIT_HOURS = {"min": 1.0 / 60.0, "h": 1.0, "d": 24.0, "wk": 168.0}


def _patient_age_years(session: Session, patient_ref: str) -> float | None:
    """Age in years from the Patient record's birthDate, or None when
    the patient or the birthDate is missing/unparseable — callers
    treat None as cannot-evaluate for age-banded rules, never as 0."""
    patient_id = patient_ref.split("/", 1)[1] if "/" in patient_ref \
        else patient_ref
    patient = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Patient",
            FhirResource.resource_id == patient_id)).first()
    if patient is None:
        return None
    dob = _parse_dt((patient.body or {}).get("birthDate"))
    if dob is None:
        return None
    age = (datetime.now(timezone.utc) - dob).days / 365.25
    return age if 0 <= age < 140 else None


def _latest_weight(session: Session, patient_ref: str):
    """(value, unit) of the patient's latest RELEASED body-weight
    Observation (LOINC 29463-7), or None. Like _latest_result, but the
    unit rides along: a weight recorded in a non-kg unit cannot be
    silently treated as kilograms."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Observation",
            FhirResource.patient_ref == patient_ref)).all()
    best = None
    for row in rows:
        body = row.body or {}
        if fhir.has_unreleased_tag(body):
            continue
        concept = body.get("code") or {}
        codes = [c.get("code") for c in concept.get("coding") or []
                 if isinstance(c, dict)]
        if WEIGHT_LOINC not in codes:
            continue
        quantity = body.get("valueQuantity") or {}
        value = quantity.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        effective = _parse_dt(body.get("effectiveDateTime")) \
            or _parse_dt(body.get("issued"))
        if effective is None:
            created = row.created_at
            effective = created if created.tzinfo is not None \
                else created.replace(tzinfo=timezone.utc)
        if best is None or effective > best[2]:
            best = (float(value),
                    quantity.get("unit") or quantity.get("code"),
                    effective)
    return (best[0], best[1]) if best is not None else None


def dose_quantity_of(order_body: dict) -> tuple[float | None, str | None]:
    """(value, unit) of an order body's structured dose: the first
    dosageInstruction's doseQuantity, direct or under doseAndRate —
    the same two shapes the Phase 11 BCMA check reads."""
    instructions = (order_body or {}).get("dosageInstruction") or []
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


def doses_per_day_of(order_body: dict) -> float | None:
    """Doses per day from an order body's FHIR dosage timing
    (frequency per period), or None when the order carries no usable
    timing. Mirrors the MAR interval maths in app.orders."""
    instructions = (order_body or {}).get("dosageInstruction") or []
    if instructions and isinstance(instructions[0], dict):
        repeat = ((instructions[0].get("timing") or {}).get("repeat") or {})
        frequency = repeat.get("frequency")
        period = repeat.get("period")
        unit = repeat.get("periodUnit") or "h"
        if isinstance(frequency, (int, float)) \
                and not isinstance(frequency, bool) \
                and isinstance(period, (int, float)) \
                and not isinstance(period, bool) \
                and frequency > 0 and period > 0 \
                and unit in _TIMING_UNIT_HOURS:
            interval_hours = float(period) * _TIMING_UNIT_HOURS[unit] \
                / float(frequency)
            return 24.0 / interval_hours if interval_hours > 0 else None
    return None


def _num(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _weight_based_evaluation(session: Session, patient_ref: str,
                             rule: dict, dose_value, dose_unit,
                             doses_per_day) -> dict:
    """Evaluate ONE rule's weightBasedDosing section for one patient
    + dose. Returns an evaluation entry whose `status` is one of
    within-range / violation / cannot-evaluate / not-applicable;
    `problem` is set for violation and cannot-evaluate (the two
    statuses that block/flag), None otherwise."""
    wb = rule.get("weightBasedDosing")
    if not isinstance(wb, dict):
        wb = {}
    # Flat bound fields may also live on the rule itself.
    merged = {**{k: rule[k] for k in
                 ("minMgPerKgPerDose", "maxMgPerKgPerDose",
                  "minMgPerKgPerDay", "maxMgPerKgPerDay")
                 if k in rule}, **wb}
    entry: dict = {"medication_code": str(rule.get("medicationCode")),
                   "medication_display": rule.get("medicationDisplay"),
                   "rule_type": "weight-based", "lab_code": None,
                   "status": "cannot-evaluate", "problem": None,
                   "weight_kg": None, "dose_mg": dose_value,
                   "mg_per_kg_per_dose": None, "mg_per_kg_per_day": None,
                   "age_years": None, "age_band": None, "bounds": None,
                   "frequency_source": None}

    def cannot(reason: str) -> dict:
        entry["problem"] = (f"weight-based (mg/kg) dose check cannot be "
                             f"evaluated: {reason}")
        return entry

    bounds = merged
    bands = merged.get("ageBands")
    if isinstance(bands, list) and bands:
        age = _patient_age_years(session, patient_ref)
        if age is None:
            return cannot("the patient has no usable birthDate, so no "
                          "age band of the published rule applies")
        entry["age_years"] = round(age, 2)
        matched = None
        for band in bands:
            if not isinstance(band, dict):
                continue
            low = _num(band.get("minAgeYears"))
            high = _num(band.get("maxAgeYears"))
            if low is not None and age < low:
                continue
            if high is not None and age >= high:
                continue
            matched = band
            break
        if matched is None:
            entry["status"] = "not-applicable"
            return entry
        entry["age_band"] = matched.get("ageBand")
        bounds = matched
    entry["bounds"] = {
        k: _num(bounds.get(k)) for k in
        ("minMgPerKgPerDose", "maxMgPerKgPerDose",
         "minMgPerKgPerDay", "maxMgPerKgPerDay")
        if _num(bounds.get(k)) is not None}
    weight = _latest_weight(session, patient_ref)
    if weight is None:
        return cannot(f"no released body-weight Observation (LOINC "
                      f"{WEIGHT_LOINC}) is recorded for the patient — "
                      "record a current weight before this medication "
                      "can be checked")
    weight_value, weight_unit = weight
    if weight_unit is not None \
            and str(weight_unit).strip().lower() not in _KG_UNITS:
        return cannot(f"the recorded body weight is in unit "
                      f"'{weight_unit}', not kg")
    if weight_value <= 0:
        return cannot(f"the recorded body weight is {weight_value} kg")
    entry["weight_kg"] = weight_value
    if dose_value is None:
        return cannot("the order carries no structured dose "
                      "(dosageInstruction doseQuantity), so no mg/kg "
                      "dose can be computed")
    expected_unit = str(merged.get("doseUnit") or "mg")
    if dose_unit is not None \
            and str(dose_unit).strip().lower() not in _MG_UNITS \
            and str(dose_unit).strip().lower() != expected_unit.lower():
        return cannot(f"the ordered dose unit is '{dose_unit}', not "
                      f"'{expected_unit}' — no unit conversion is "
                      "assumed for a safety check")
    per_dose = dose_value / weight_value
    entry["mg_per_kg_per_dose"] = round(per_dose, 3)
    violations = []
    b = entry["bounds"]
    if b.get("minMgPerKgPerDose") is not None \
            and per_dose < b["minMgPerKgPerDose"]:
        violations.append(
            f"dose {dose_value:g} mg is {per_dose:.2f} mg/kg/dose for a "
            f"{weight_value:g} kg patient, below the published minimum "
            f"{b['minMgPerKgPerDose']:g} mg/kg/dose")
    if b.get("maxMgPerKgPerDose") is not None \
            and per_dose > b["maxMgPerKgPerDose"]:
        violations.append(
            f"dose {dose_value:g} mg is {per_dose:.2f} mg/kg/dose for a "
            f"{weight_value:g} kg patient, above the published maximum "
            f"{b['maxMgPerKgPerDose']:g} mg/kg/dose")
    if b.get("minMgPerKgPerDay") is not None \
            or b.get("maxMgPerKgPerDay") is not None:
        if doses_per_day is None:
            frequency, entry["frequency_source"] = 1.0, "assumed-once-daily"
        else:
            frequency, entry["frequency_source"] = doses_per_day, "order-timing"
        per_day = per_dose * frequency
        entry["mg_per_kg_per_day"] = round(per_day, 3)
        if b.get("minMgPerKgPerDay") is not None \
                and per_day < b["minMgPerKgPerDay"]:
            violations.append(
                f"dose is {per_day:.2f} mg/kg/day "
                f"({entry['frequency_source']}), below the published "
                f"minimum {b['minMgPerKgPerDay']:g} mg/kg/day")
        if b.get("maxMgPerKgPerDay") is not None \
                and per_day > b["maxMgPerKgPerDay"]:
            violations.append(
                f"dose is {per_day:.2f} mg/kg/day "
                f"({entry['frequency_source']}), above the published "
                f"maximum {b['maxMgPerKgPerDay']:g} mg/kg/day")
    if violations:
        entry["status"] = "violation"
        entry["problem"] = ("weight-based (mg/kg) dose violation: "
                            + "; ".join(violations))
    else:
        entry["status"] = "within-range"
    return entry


def weight_based_dose_check(session: Session, patient_ref: str,
                            medication_code: str, dose_value=None,
                            dose_unit=None, doses_per_day=None):
    """Evaluate the published weight-based (mg/kg) rules for one
    medication + patient + dose. Returns (blockers, warnings,
    evaluations): violations and cannot-evaluate results ride
    blockers/warnings per the rule's action (the medication-
    monitoring mechanism); every evaluation — including within-range
    and not-applicable — is returned so callers can surface the
    explicit status instead of a silent pass."""
    blockers: list[dict] = []
    warnings: list[dict] = []
    evaluations: list[dict] = []
    for rule in _monitoring_rules(session):
        if str(rule.get("medicationCode")) != str(medication_code):
            continue
        if not isinstance(rule.get("weightBasedDosing"), dict) \
                and not any(k in rule for k in
                            ("minMgPerKgPerDose", "maxMgPerKgPerDose",
                             "minMgPerKgPerDay", "maxMgPerKgPerDay")):
            continue
        action = rule.get("action") if rule.get("action") in ("block", "flag") \
            else "block"
        evaluation = _weight_based_evaluation(
            session, patient_ref, rule, dose_value, dose_unit, doses_per_day)
        evaluations.append(evaluation)
        if evaluation["status"] in ("violation", "cannot-evaluate"):
            (blockers if action == "block" else warnings).append(evaluation)
    return blockers, warnings, evaluations


def medication_monitoring_check(session: Session, patient_ref: str,
                                medication_code: str, dose_value=None,
                                dose_unit=None, doses_per_day=None
                                ) -> tuple[list[dict], list[dict]]:
    """Evaluate the published monitoring rules for one dispense.
    Returns (blockers, warnings): each entry names the rule's
    medication, the lab and the problem. An empty rules config (the
    default) returns two empty lists — the gate is inert. When the
    caller supplies the order's dose context, weight-based (mg/kg)
    rules are evaluated too and their violations / cannot-evaluate
    results merge into the same two lists (Phase 17, BD-08)."""
    blockers: list[dict] = []
    warnings: list[dict] = []
    for rule in _monitoring_rules(session):
        if str(rule.get("medicationCode")) != str(medication_code):
            continue
        action = rule.get("action") if rule.get("action") in ("block", "flag") \
            else "block"
        for lab in rule.get("requiredLabs") or []:
            if not isinstance(lab, dict) or not lab.get("code"):
                continue
            problem = _monitoring_problem(session, patient_ref, lab)
            if problem is None:
                continue
            entry = {"medication_code": str(medication_code),
                     "medication_display": rule.get("medicationDisplay"),
                     "lab_code": str(lab.get("code")),
                     "problem": problem}
            (blockers if action == "block" else warnings).append(entry)
    wb_blockers, wb_warnings, _ = weight_based_dose_check(
        session, patient_ref, medication_code,
        dose_value=dose_value, dose_unit=dose_unit,
        doses_per_day=doses_per_day)
    blockers.extend(wb_blockers)
    warnings.extend(wb_warnings)
    return blockers, warnings


def _monitoring_block_message(blockers: list[dict]) -> str:
    meds = ", ".join(sorted({b["medication_display"] or b["medication_code"]
                             for b in blockers}))
    problems = "; ".join(b["problem"] for b in blockers)
    if any(b.get("rule_type") == "weight-based" for b in blockers):
        return (f"Medication monitoring block: {meds} failed a published "
                f"safety check before dispensing — {problems}. Resolve "
                "the named problem (record the required results / "
                "weight, or correct the dose) before this medication "
                "can be dispensed")
    return (f"Medication monitoring block: {meds} requires current "
            f"monitoring labs before dispensing — {problems}. Record "
            "the required results (and have them released) before "
            "this medication can be dispensed")


# ---------------------------------------------------------------------------
# Formulary / stock / dispense queue reads (staff)
# ---------------------------------------------------------------------------

@router.get("/formulary")
def get_formulary(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("pharmacy.read"))):
    cfg = published_config(session, "terminology", FORMULARY_KEY)
    return {"formulary": _formulary(session),
            "version": cfg.version if cfg else None}


@router.get("/stock")
def get_stock(session: Session = Depends(get_db),
              user: User = Depends(require_capability("pharmacy.read"))):
    return {"items": _stock_out(session)}


def _verification_annotation(session: Session, order_id: str) -> dict:
    """The Wave A verification state of one order for queue display:
    {verified, verified_by, verified_at, state, reason} — the single
    source is app.pharmacy_ops.verification_state (imported lazily:
    pharmacy_ops imports this module, so a top-level import would be
    circular)."""
    from .pharmacy_ops import verification_state
    state = verification_state(session, order_id)
    record = state["verification"]
    return {
        "verified": state["state"] == "current",
        "verified_by": record.verified_by if record is not None else None,
        "verified_at": (record.verified_at.isoformat()
                        if record is not None and record.verified_at
                        else None),
        "state": state["state"],
        "reason": state["reason"],
    }


@router.get("/dispense-queue")
def dispense_queue(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("pharmacy.read"))):
    """Active MedicationRequests awaiting dispensing, each with its
    formulary entry and live stock level, oldest first. Wave A
    (PHARM-B1): each item also carries its verification state —
    dispensing refuses orders whose verification is not CURRENT, and
    the queue says so (with the reason) before anyone clicks."""
    rows = session.scalars(
        select(FhirResource)
        .where(FhirResource.resource_type == "MedicationRequest",
               FhirResource.status == "active")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    stock = {item["code"]: item for item in _stock_out(session)}
    items = []
    for row in rows:
        _, code, _ = _medication_of(row.body or {})
        entry = stock.get(code or "")
        items.append({
            "request": fhir._render(row),
            "medication_code": code,
            "in_formulary": entry is not None,
            "stock_tracked": entry["stock_tracked"] if entry else None,
            "stock_quantity": entry["quantity"] if entry else None,
            "verification": _verification_annotation(session,
                                                     row.resource_id),
        })
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Dispensing
# ---------------------------------------------------------------------------

@router.post("/dispense", status_code=201)
async def dispense(request: Request, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("pharmacy.dispense"))):
    """Dispense an active MedicationRequest: create the MedicationDispense,
    decrement stock (stock-tracked meds only), and move the request to
    'dispensed'. Dispensing more than the on-hand stock -> 409 with
    nothing written; a request that is not active -> 409; a medication
    that is not in the published formulary -> 422. Wave A (PHARM-B1):
    an order without a CURRENT pharmacist verification -> 409 naming
    the verification state, audited, with nothing written."""
    payload = await _json_body(request)
    request_id = (payload.get("medication_request_id") or payload.get("request_id")
                  or payload.get("medicationRequestId"))
    if not request_id and isinstance(payload.get("medicationRequest"), dict):
        reference = payload["medicationRequest"].get("reference") or ""
        request_id = reference.split("/", 1)[1] if "/" in reference else reference
    if not request_id:
        raise HTTPException(status_code=422,
                            detail="Dispense requires 'medication_request_id'")
    med_request = fhir._get_row("MedicationRequest", str(request_id), session)
    request_body = med_request.body or {}
    if request_body.get("status") != "active":
        raise HTTPException(
            status_code=409,
            detail=(f"MedicationRequest/{request_id} is "
                    f"'{request_body.get('status')}', not 'active'; only an active "
                    "request can be dispensed"))
    _, code, concept = _medication_of(request_body)
    if not code:
        raise HTTPException(status_code=422,
                            detail="MedicationRequest carries no medication code")
    entry = _formulary_entry(session, code)
    if entry is None or entry.get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Medication code '{code}' is not an active entry in the "
                   "published formulary")
    quantity = payload.get("quantity", 1)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise HTTPException(status_code=422,
                            detail="Dispense 'quantity' must be a positive integer")

    # Medication monitoring gate (Phase 9): published rules may BLOCK
    # this dispense (409, nothing written — the QC-gate posture) or
    # FLAG it (warnings ride the response + audit). Inert when no
    # monitoring config is published.
    patient_ref = (request_body.get("subject") or {}).get("reference") or ""
    # Phase 17 (BD-08): the order's dose context rides along so
    # weight-based (mg/kg) rules evaluate at the same gate.
    dose_value, dose_unit = dose_quantity_of(request_body)
    blockers, monitoring_warnings = medication_monitoring_check(
        session, patient_ref, code, dose_value=dose_value,
        dose_unit=dose_unit, doses_per_day=doses_per_day_of(request_body))
    if blockers:
        raise HTTPException(status_code=409,
                            detail=_monitoring_block_message(blockers))

    stock_row = None
    if entry.get("stockTracked"):
        stock_row = _stock_row(session, code, entry)
        if stock_row.quantity < quantity:
            raise HTTPException(
                status_code=409,
                detail=(f"Insufficient stock for '{code}' "
                        f"({entry.get('display')}): {stock_row.quantity} on hand, "
                        f"{quantity} requested"))

    # Wave A (PHARM-B1) — the verification gate. Every check above is
    # read-only; this is the last check BEFORE the first write (the
    # FEFO allocation below mutates lots). Dispensing requires a
    # CURRENT pharmacist verification of this order (single source:
    # app.pharmacy_ops.verification_state — imported lazily to avoid
    # the pharmacy <-> pharmacy_ops import cycle). A refusal is
    # audited and committed BEFORE the 409 is raised (the BCMA
    # refuse() posture: the chain of custody includes refusals), and
    # changes nothing else — no dispense row, no stock movement, the
    # request stays active.
    from .pharmacy_ops import verification_state
    vstate = verification_state(session, str(request_id))
    if vstate["state"] != "current":
        detail = (
            f"Dispensing refused: MedicationRequest/{request_id} has no "
            f"current pharmacist verification — {vstate['reason']}. "
            "The order must be verified (Verification & DUR queue) "
            "before it can be dispensed")
        append_audit(session, actor=user.username,
                     action="pharmacy.dispense.refused_unverified",
                     entity=f"MedicationRequest/{request_id}",
                     payload={"medication_request_id": str(request_id),
                              "medication_code": code,
                              "verification_state": vstate["state"],
                              "reason": vstate["reason"],
                              "detail": detail})
        session.commit()
        raise HTTPException(status_code=409, detail=detail)

    # Phase 11: FEFO lot allocation at the configured dispense location
    # (no-op for aggregate-only stock — see _allocate_lots).
    lot_number = None
    serials: list[str] = []
    taken: list[tuple[str, int]] = []
    if stock_row is not None:
        lot_number, serials, taken = _allocate_lots(
            session, code, quantity, _dispense_location(session))

    dispense_body = {
        "resourceType": "MedicationDispense",
        "id": str(uuid.uuid4()),
        "status": "completed",
        "medicationCodeableConcept": concept,
        "subject": request_body.get("subject"),
        "authorizingPrescription": [
            {"reference": f"MedicationRequest/{request_id}"}],
        "quantity": {"value": quantity, "unit": "unit"},
        "whenHandedOver": _now_iso(),
        "performer": [{"actor": {"display": user.username}}],
    }
    if request_body.get("encounter"):
        dispense_body["encounter"] = request_body["encounter"]
    # The dispense carries the order's dosage instructions (dose/route)
    # so the Phase 11 BCMA five-rights check compares what was handed
    # over against what was ordered, from the dispense record itself.
    if request_body.get("dosageInstruction"):
        dispense_body["dosageInstruction"] = request_body["dosageInstruction"]
    extensions = []
    if lot_number:
        extensions.append({"url": LOT_EXTENSION, "valueString": lot_number})
    if serials:
        extensions.append({"url": SERIAL_EXTENSION,
                           "valueString": ", ".join(serials)})
    if extensions:
        dispense_body["extension"] = extensions
    rendered = fhir.create_resource("MedicationDispense", dispense_body, session, user)

    stock_after = None
    if stock_row is not None:
        stock_row.quantity -= quantity
        stock_after = stock_row.quantity
        session.flush()
    if lot_number:
        session.add(DscsaLedgerEntry(
            entry_id=f"dscsa-{uuid.uuid4()}", event="dispense",
            medication_code=code, lot_number=lot_number,
            serial=serials[0] if serials else None,
            quantity=quantity,
            reference=f"MedicationDispense/{rendered['id']}",
            detail={"serials": serials,
                    "lots": [{"lot": lot, "quantity": qty}
                             for lot, qty in taken],
                    "location": _dispense_location(session)},
            actor=user.username))
        session.flush()
    body = dict(request_body)
    body["status"] = "dispensed"
    med_request.body = body
    med_request.status = "dispensed"
    med_request.version += 1
    session.flush()
    # Wave A (PHARM-B1): the dispense's own status transition bumps
    # the order version — the verification that authorized THIS
    # dispense is re-bound across it, so the BCMA gate at
    # administration time still sees the verification as current for
    # the order as dispensed. Any OTHER edit to the order (before
    # dispensing, or after it) moves the version again and the
    # verification goes stale, exactly as designed.
    _vrecord = vstate["verification"]
    if _vrecord is not None:
        _vrecord.order_version = med_request.version
        session.flush()
    append_audit(session, actor=user.username, action="pharmacy.dispense",
                 entity=f"MedicationDispense/{rendered['id']}",
                 payload={"medication_request_id": str(request_id),
                          "medication_code": code, "quantity": quantity,
                          "stock_tracked": bool(entry.get("stockTracked")),
                          "stock_after": stock_after,
                          "lot_number": lot_number, "serials": serials,
                          "monitoring_warnings": monitoring_warnings})
    session.commit()
    return {"dispense": rendered,
            "medication_request": fhir._render(med_request),
            "stock_after": stock_after,
            "monitoring_warnings": monitoring_warnings}


@router.get("/dispenses")
def list_dispenses(patient: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("pharmacy.read"))):
    stmt = select(FhirResource).where(
        FhirResource.resource_type == "MedicationDispense")
    if patient:
        stmt = stmt.where(FhirResource.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.order_by(FhirResource.created_at.asc(),
                                         FhirResource.id.asc())).all()
    return fhir._bundle(rows)


# ---------------------------------------------------------------------------
# Administration bookkeeping shared with app.pharmacy_ops
#
# Wave A (PHARM-M2) retired this module's BCMA-lite endpoint (see the
# module docstring): the ONE administration engine is the five-rights
# BCMA scan in app.pharmacy_ops. This helper stays here because the
# BCMA engine consumes it (a dispense is administered once).
# ---------------------------------------------------------------------------

def _administered_dispense_ids(session: Session) -> set[str]:
    ids = set()
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "MedicationAdministration")).all()
    for row in rows:
        for ext in (row.body or {}).get("extension") or []:
            if isinstance(ext, dict) and ext.get("url") == DISPENSE_EXTENSION:
                reference = ((ext.get("valueReference") or {}).get("reference") or "")
                if reference.startswith("MedicationDispense/"):
                    ids.add(reference.split("/", 1)[1])
    return ids
