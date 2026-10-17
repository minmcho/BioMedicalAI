"""Phase 7 — Revenue cycle completion: HIM/coding, X12 835 remittance
posting, denials workqueue, patient statements + recorded payments.

Built ON the Phase 4A billing module (app.billing) and the Studio
customization engine like everything else (roadmap §6):

- the coding value sets are terminology config — keys
  ``coding-diagnoses`` (ICD-10-CM subset) and ``coding-procedures``
  (CPT subset); an unknown code is refused 422, and systems/displays
  are resolved from the sets server-side;
- the 835 CLP02 -> claim-status map is terminology config, key
  ``remittance-status``; an unmapped CLP02 is an exception, never a
  guess;
- denial reason categories (by CARC reason code) are terminology
  config, key ``denial-categories`` (which also carries the workqueue
  `outcomes` list); reason codes themselves are stored VERBATIM from
  the remittance CAS segments;
- patient payment methods are terminology config, key
  ``payment-methods``;
- whether coding is REQUIRED before claim generation is the claim
  lifecycle config's `requireCoding` flag (app.billing) — off by
  default, so pre-Phase-7 behaviour is preserved exactly;

all seeded as published v1 at startup by :func:`ensure_revenue_defaults`.

835 SCOPE, stated plainly (the Phase 4A honesty rule, extended):
posting is a LOCAL ingest of an 835 file handed to this endpoint —
there is NO clearinghouse connection and no payer submission. The
parser is real for the segments it consumes (ST/SE envelope with the
SE count validated, BPR, TRN, N1/NM1 parties, CLP claim loops, SVC
service lines, CAS adjustments) and refuses a malformed file whole:
nothing posts. A well-formed file whose individual CLP loops cannot
post (unknown claim, unmapped status, claim not yet submitted,
duplicate check) records those loops as EXCEPTIONS on the batch —
never silently dropped — and posts the rest.

Remittance posting writes claim status DIRECTLY (with a status-history
entry and a hash-chained audit row), bypassing the human claim
lifecycle machine on purpose: an 835 is a payer system event, not a
user transition (the Phase 6B auto-verify actor pattern). Outcomes
come from the published status map: paid / denied, with `paid`
becoming `partially-paid` when money is still outstanding after the
posting (paid + non-patient-responsibility adjustments + patient
responsibility < the claim total). A site that wants lifecycle
transitions out of `partially-paid` publishes them in the
claim-lifecycle config — states are data there.

PATIENT PAYMENTS are ledger records of payments the practice took
(portal "bill pay" in v1 RECORDS a payment) — there is no card
processor or payment gateway in this system, and the API/UI say so.
"""
import copy
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import billing, fhir
from .audit import append_audit
from .configstore import published_config, role_allowed
from .models import (CodingRecord, ConfigStatus, DenialWorkItem, FhirResource,
                     PatientPayment, RemittanceBatch, RemittanceException,
                     Role, StudioConfig, User)
from .security import get_current_user, get_db, require_capability

router = APIRouter(prefix="/billing", tags=["billing-revenue"])

CODING_DIAGNOSES_KEY = "coding-diagnoses"
CODING_PROCEDURES_KEY = "coding-procedures"
DENIAL_CATEGORIES_KEY = "denial-categories"
REMITTANCE_STATUS_KEY = "remittance-status"
PAYMENT_METHODS_KEY = "payment-methods"

ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
CPT_SYSTEM = "http://www.ama-assn.org/go/cpt"
REMITTANCE_CATEGORY_CODE = "remittance"
REBILL_EXTENSION = "urn:opencare:extension/rebill-of"
DEFAULT_DENIAL_OUTCOMES = ("appealed", "rebilled", "written-off", "closed")
PAYMENT_DISCLAIMER = (
    "Payments recorded here are ledger entries for payments the "
    "practice has taken; this system has no card processor or "
    "payment gateway.")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _money(value) -> float:
    return round(float(value), 2)


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def default_coding_diagnoses_payload() -> dict:
    def dx(code, display):
        return {"code": code, "display": display, "active": True,
                "system": ICD10_SYSTEM}

    return {"name": "OpenCare coding diagnoses (ICD-10-CM subset, synthetic)",
            "system": ICD10_SYSTEM,
            "concepts": [
                dx("E11.9", "Type 2 diabetes mellitus without complications"),
                dx("I10", "Essential (primary) hypertension"),
                dx("J45.909", "Unspecified asthma, uncomplicated"),
                dx("E78.5", "Hyperlipidemia, unspecified"),
                dx("M17.0", "Bilateral primary osteoarthritis of knee"),
                dx("R07.9", "Chest pain, unspecified"),
                dx("K21.9", "Gastro-esophageal reflux disease without esophagitis"),
                dx("H26.9", "Unspecified cataract"),
                dx("Z00.00", "Encounter for general adult medical examination"),
            ]}


def default_coding_procedures_payload() -> dict:
    def px(code, display):
        return {"code": code, "display": display, "active": True,
                "system": CPT_SYSTEM}

    return {"name": "OpenCare coding procedures (CPT subset, synthetic)",
            "system": CPT_SYSTEM,
            "concepts": [
                px("99213", "Office visit, established patient (low)"),
                px("99214", "Office visit, established patient (moderate)"),
                px("93005", "Electrocardiogram"),
                px("71046", "Chest X-ray, 2 views"),
                px("20610", "Arthrocentesis, knee"),
                px("90791", "Psychiatric diagnostic evaluation"),
                px("94640", "Inhalation therapy administration"),
            ]}


def default_denial_categories_payload() -> dict:
    def cat(code, display, category):
        return {"code": code, "display": display, "active": True,
                "category": category}

    return {"name": "OpenCare denial reason categories (CARC subset, synthetic)",
            "system": "urn:opencare:denial-category",
            "outcomes": list(DEFAULT_DENIAL_OUTCOMES),
            "concepts": [
                cat("197", "Precertification/authorization/notification absent",
                    "authorisation"),
                cat("96", "Non-covered charge(s)", "coverage"),
                cat("50", "Not deemed medically necessary", "medical-necessity"),
                cat("16", "Claim/service lacks information", "information"),
                cat("4", "Procedure code inconsistent with modifier used", "coding"),
                cat("45", "Charge exceeds fee schedule maximum", "contractual"),
            ]}


def default_remittance_status_payload() -> dict:
    def st(code, display, status):
        return {"code": code, "display": display, "active": True,
                "claimStatus": status}

    return {"name": "X12 835 CLP02 claim status map",
            "system": "urn:opencare:835-clp02",
            "concepts": [
                st("1", "Processed as primary", "paid"),
                st("2", "Processed as secondary", "paid"),
                st("3", "Processed as tertiary", "paid"),
                st("4", "Denied", "denied"),
                st("19", "Processed as primary, forwarded to additional payer(s)",
                   "paid"),
            ]}


def default_payment_methods_payload() -> dict:
    def pm(code, display):
        return {"code": code, "display": display, "active": True}

    return {"name": "Patient payment methods (recorded payments)",
            "system": "urn:opencare:payment-method",
            "concepts": [
                pm("card", "Card payment (recorded)"),
                pm("ach", "Bank transfer / ACH (recorded)"),
                pm("cash", "Cash (recorded)"),
                pm("check", "Check (recorded)"),
            ]}


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


def ensure_revenue_defaults(session: Session) -> None:
    """Seed the coding value sets, denial categories, the 835 status
    map and payment methods as published Studio config v1
    (idempotent)."""
    _seed_published(session, "terminology", CODING_DIAGNOSES_KEY,
                    default_coding_diagnoses_payload())
    _seed_published(session, "terminology", CODING_PROCEDURES_KEY,
                    default_coding_procedures_payload())
    _seed_published(session, "terminology", DENIAL_CATEGORIES_KEY,
                    default_denial_categories_payload())
    _seed_published(session, "terminology", REMITTANCE_STATUS_KEY,
                    default_remittance_status_payload())
    _seed_published(session, "terminology", PAYMENT_METHODS_KEY,
                    default_payment_methods_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------

def _concepts(session: Session, key: str) -> list[dict]:
    cfg = published_config(session, "terminology", key)
    if cfg is None:
        return []
    return [c for c in (cfg.payload or {}).get("concepts") or []
            if isinstance(c, dict)]


def _concept_map(session: Session, key: str) -> dict[str, dict]:
    return {c["code"]: c for c in _concepts(session, key) if c.get("code")}


def _status_outcome(session: Session, clp02: str) -> str | None:
    concept = _concept_map(session, REMITTANCE_STATUS_KEY).get(clp02)
    if concept is None or concept.get("active") is False:
        return None
    outcome = concept.get("claimStatus")
    return outcome if isinstance(outcome, str) and outcome else None


def _denial_outcomes(session: Session) -> tuple:
    cfg = published_config(session, "terminology", DENIAL_CATEGORIES_KEY)
    values = (cfg.payload or {}).get("outcomes") if cfg is not None else None
    if isinstance(values, list) and values and all(
            isinstance(v, str) and v for v in values):
        return tuple(values)
    return DEFAULT_DENIAL_OUTCOMES


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


# ---------------------------------------------------------------------------
# HIM / coding
# ---------------------------------------------------------------------------

def _render_coding(record: CodingRecord) -> dict:
    return {"encounter_id": record.encounter_id, "patient": record.patient_ref,
            "diagnoses": record.diagnoses or [], "procedures": record.procedures or [],
            "coded_by": record.coded_by,
            "coded_at": record.created_at.isoformat() if record.created_at else None,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None}


def _signed_note_encounters(session: Session) -> dict[str, int]:
    """encounter id -> signed-note count, from DocumentReferences
    with docStatus final/amended carrying a context encounter."""
    counts: dict[str, int] = {}
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "DocumentReference")).all()
    for row in rows:
        body = row.body or {}
        if body.get("docStatus") not in ("final", "amended"):
            continue
        for ref in (body.get("context") or {}).get("encounter") or []:
            reference = (ref or {}).get("reference") or ""
            if reference.startswith("Encounter/"):
                encounter_id = reference.split("/", 1)[1]
                counts[encounter_id] = counts.get(encounter_id, 0) + 1
    return counts


@router.get("/coding/queue")
def coding_queue(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("coding.read"))):
    """Encounters awaiting HIM coding: status `finished`, or carrying
    at least one SIGNED note — and no coding record yet."""
    signed = _signed_note_encounters(session)
    coded = {r.encounter_id for r in session.scalars(select(CodingRecord)).all()}
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Encounter")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = []
    for row in rows:
        if row.resource_id in coded:
            continue
        body = row.body or {}
        finished = body.get("status") == "finished"
        note_count = signed.get(row.resource_id, 0)
        if not finished and note_count == 0:
            continue
        period = body.get("period") or {}
        items.append({
            "encounter_id": row.resource_id, "patient": row.patient_ref,
            "encounter_status": body.get("status"),
            "period_start": period.get("start"), "period_end": period.get("end"),
            "signed_notes": note_count,
            "reason": "finished" if finished else "signed-note",
        })
    return {"items": items, "total": len(items)}


def _validate_codes(session: Session, payload: dict) -> tuple[list[dict], list[dict]]:
    """Resolve coder-supplied codes against the published value sets.
    Unknown (or inactive) code -> 422 naming it; the stored entries
    carry the SET's system + display, never caller-asserted text."""
    def resolve(raw, key: str, default_system: str, field: str) -> list[dict]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise HTTPException(status_code=422, detail=f"'{field}' must be a list")
        concepts = _concept_map(session, key)
        out = []
        for i, entry in enumerate(raw):
            code = entry.get("code") if isinstance(entry, dict) else entry
            if not isinstance(code, str) or not code:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field}[{i}] must name a 'code'")
            concept = concepts.get(code)
            if concept is None or concept.get("active") is False:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown {field} code '{code}': not in the "
                           f"published terminology/{key} value set")
            out.append({"system": concept.get("system") or default_system,
                        "code": code, "display": concept.get("display")})
        return out

    diagnoses = resolve(payload.get("diagnoses"), CODING_DIAGNOSES_KEY,
                        ICD10_SYSTEM, "diagnoses")
    procedures = resolve(payload.get("procedures"), CODING_PROCEDURES_KEY,
                         CPT_SYSTEM, "procedures")
    if not diagnoses:
        raise HTTPException(
            status_code=422,
            detail="Coding an encounter requires at least one diagnosis code")
    return diagnoses, procedures


@router.post("/coding/{encounter_id}", status_code=201)
async def assign_codes(encounter_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("coding.work"))):
    """Assign the HIM codes for one encounter (audited). 409 when the
    encounter is already coded — use PUT to amend the record."""
    encounter = fhir._get_row("Encounter", encounter_id, session)
    if billing.coding_record_for(session, encounter_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Encounter/{encounter_id} already has a coding record; "
                   "use PUT /billing/coding/{encounter_id} to amend it")
    payload = await _json_body(request)
    diagnoses, procedures = _validate_codes(session, payload)
    record = CodingRecord(
        encounter_id=encounter_id,
        patient_ref=encounter.patient_ref or "",
        diagnoses=diagnoses, procedures=procedures, coded_by=user.username)
    session.add(record)
    session.flush()
    append_audit(session, actor=user.username, action="coding.assign",
                 entity=f"Encounter/{encounter_id}",
                 payload={"diagnoses": [d["code"] for d in diagnoses],
                          "procedures": [p["code"] for p in procedures],
                          "patient": record.patient_ref})
    session.commit()
    return _render_coding(record)


@router.put("/coding/{encounter_id}")
async def amend_codes(encounter_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("coding.work"))):
    """Amend an existing coding record (codes re-validated against
    the published sets; the amendment is audited)."""
    record = billing.coding_record_for(session, encounter_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Encounter/{encounter_id} has no coding record to amend")
    payload = await _json_body(request)
    diagnoses, procedures = _validate_codes(session, payload)
    record.diagnoses = diagnoses
    record.procedures = procedures
    record.coded_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="coding.amend",
                 entity=f"Encounter/{encounter_id}",
                 payload={"diagnoses": [d["code"] for d in diagnoses],
                          "procedures": [p["code"] for p in procedures]})
    session.commit()
    return _render_coding(record)


@router.get("/coding/{encounter_id}")
def get_codes(encounter_id: str, session: Session = Depends(get_db),
              user: User = Depends(require_capability("coding.read"))):
    record = billing.coding_record_for(session, encounter_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Encounter/{encounter_id} has no coding record")
    return _render_coding(record)


# ---------------------------------------------------------------------------
# X12 835 remittance: parser
# ---------------------------------------------------------------------------

class EdiError(Exception):
    """A malformed 835 file. Always raised BEFORE anything posts, so a
    bad file posts nothing (all-or-nothing at the file level)."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _edi_amount(elements: list[str], index: int, segment_no: int,
                segment_id: str) -> float:
    raw = elements[index] if index < len(elements) else ""
    try:
        return _money(Decimal(raw))
    except (InvalidOperation, ValueError, ArithmeticError):
        raise EdiError(
            f"Malformed 835: segment {segment_no} ({segment_id}) element "
            f"{index + 1} is not a valid amount: '{raw}'")


def parse_835(text: str) -> dict:
    """Parse the 835 subset this system posts: ISA/GS envelope
    (tolerated, not interpreted), ST/SE (SE count validated), BPR
    total, TRN check number, N1/NM1 parties, and per-claim CLP loops
    with SVC lines and CAS adjustments. Returns
    {check_number, payer, total_amount, claims: [...]}."""
    if not isinstance(text, str) or not text.strip():
        raise EdiError("Malformed 835: the payload is empty")
    raw_segments = [s.strip() for s in text.replace("\r", " ").split("~")]
    segments = [s.split("*") for s in raw_segments if s]
    segments = [[el.strip() for el in seg] for seg in segments]
    if not segments:
        raise EdiError("Malformed 835: no segments found")

    st_index = next((i for i, seg in enumerate(segments) if seg[0] == "ST"), None)
    if st_index is None:
        raise EdiError("Malformed 835: no ST segment found")
    st = segments[st_index]
    if len(st) < 2 or st[1] != "835":
        raise EdiError(
            f"Malformed 835: ST segment names transaction "
            f"'{st[1] if len(st) > 1 else ''}', not '835'")
    se_index = next((i for i in range(st_index, len(segments))
                     if segments[i][0] == "SE"), None)
    if se_index is None:
        raise EdiError("Malformed 835: no SE segment found after ST")
    se = segments[se_index]
    actual = se_index - st_index + 1
    try:
        declared = int(se[1])
    except (IndexError, ValueError):
        raise EdiError("Malformed 835: SE segment carries no valid segment count")
    if declared != actual:
        raise EdiError(
            f"Malformed 835: SE count declares {declared} segments but "
            f"{actual} are present (ST through SE)")

    body = segments[st_index:se_index + 1]
    result: dict = {"check_number": "", "payer": "", "total_amount": 0.0,
                    "claims": []}
    current_claim: dict | None = None
    current_line: dict | None = None

    def flush_claim() -> None:
        nonlocal current_claim, current_line
        if current_claim is not None:
            result["claims"].append(current_claim)
        current_claim = None
        current_line = None

    for offset, seg in enumerate(body):
        seg_no = st_index + offset + 1  # 1-based, for error messages
        seg_id = seg[0]
        if seg_id == "BPR":
            result["total_amount"] = _edi_amount(seg, 2, seg_no, "BPR")
        elif seg_id == "TRN":
            result["check_number"] = seg[2] if len(seg) > 2 else ""
        elif seg_id in ("N1", "NM1"):
            entity = seg[1] if len(seg) > 1 else ""
            name = seg[3] if seg_id == "NM1" and len(seg) > 3 else \
                (seg[2] if len(seg) > 2 else "")
            if entity == "PR" and name:
                result["payer"] = name
        elif seg_id == "CLP":
            flush_claim()
            if len(seg) < 6:
                raise EdiError(
                    f"Malformed 835: segment {seg_no} (CLP) has "
                    f"{len(seg) - 1} elements; a CLP loop needs at least "
                    "claim id, status, charge, payment and patient "
                    "responsibility")
            if not seg[1]:
                raise EdiError(
                    f"Malformed 835: segment {seg_no} (CLP) has an empty "
                    "claim id (CLP01)")
            if not seg[2]:
                raise EdiError(
                    f"Malformed 835: segment {seg_no} (CLP) has an empty "
                    "claim status code (CLP02)")
            current_claim = {
                "claim_ref": seg[1], "status_code": seg[2],
                "charge": _edi_amount(seg, 3, seg_no, "CLP"),
                "paid": _edi_amount(seg, 4, seg_no, "CLP"),
                "patient_responsibility": _edi_amount(seg, 5, seg_no, "CLP"),
                "lines": [], "adjustments": [],
            }
        elif seg_id == "SVC":
            if current_claim is None:
                raise EdiError(
                    f"Malformed 835: segment {seg_no} (SVC) appears "
                    "outside a CLP loop")
            composite = seg[1] if len(seg) > 1 else ""
            code = composite.split(":", 1)[1] if ":" in composite else composite
            current_line = {
                "code": code,
                "charge": _edi_amount(seg, 2, seg_no, "SVC"),
                "paid": _edi_amount(seg, 3, seg_no, "SVC"),
                "adjustments": [],
            }
            current_claim["lines"].append(current_line)
        elif seg_id == "CAS":
            if current_claim is None:
                raise EdiError(
                    f"Malformed 835: segment {seg_no} (CAS) appears "
                    "outside a CLP loop")
            if len(seg) < 4:
                raise EdiError(
                    f"Malformed 835: segment {seg_no} (CAS) needs group, "
                    "reason and amount elements")
            adjustment = {"group": seg[1], "reason": seg[2],
                          "amount": _edi_amount(seg, 3, seg_no, "CAS")}
            target = current_line if current_line is not None else current_claim
            target["adjustments"].append(adjustment)
    flush_claim()
    return result


# ---------------------------------------------------------------------------
# Remittance posting + claim financials
# ---------------------------------------------------------------------------

def remittance_entries(body: dict) -> list[dict]:
    """The posted-remittance records on a claim (supportingInfo
    category `remittance` — the claim's status history uses the
    sibling code `status-history`, see app.billing)."""
    entries = []
    for info in body.get("supportingInfo") or []:
        if not isinstance(info, dict):
            continue
        category = ((info.get("category") or {}).get("coding") or [{}])[0]
        if category.get("system") != billing.STATUS_HISTORY_CATEGORY:
            continue
        if category.get("code") != REMITTANCE_CATEGORY_CODE:
            continue
        try:
            entries.append(json.loads(info.get("valueString") or "{}"))
        except (TypeError, ValueError):
            continue
    return entries


def _append_remittance(body: dict, entry: dict) -> None:
    infos = list(body.get("supportingInfo") or [])
    infos.append({
        "sequence": len(infos) + 1,
        "category": {"coding": [{"system": billing.STATUS_HISTORY_CATEGORY,
                                 "code": REMITTANCE_CATEGORY_CODE,
                                 "display": "Remittance posting"}]},
        "valueString": json.dumps(entry, sort_keys=True),
    })
    body["supportingInfo"] = infos


def _all_adjustments(entry: dict) -> list[dict]:
    out = []
    for adj in entry.get("adjustments") or []:
        out.append({**adj, "level": "claim", "line_code": None})
    for line in entry.get("lines") or []:
        for adj in line.get("adjustments") or []:
            out.append({**adj, "level": "line", "line_code": line.get("code")})
    return out


def claim_financials(session: Session, claim_id: str, body: dict) -> dict:
    """Cumulative money view of one claim: billed total, remittance
    paid / adjustments (excluding patient-responsibility PR amounts,
    which are tracked as the patient's share) / patient responsibility,
    recorded patient payments, the outstanding claim balance and the
    amount the patient still owes. All figures derive from posted
    records — nothing is caller-asserted."""
    total = _money(((body.get("total") or {}).get("value")) or 0.0)
    entries = remittance_entries(body)
    paid = _money(sum(e.get("paid") or 0.0 for e in entries))
    adjustments = _money(sum(
        adj.get("amount") or 0.0
        for e in entries for adj in _all_adjustments(e)
        if adj.get("group") != "PR"))
    patient_responsibility = _money(sum(
        e.get("patient_responsibility") or 0.0 for e in entries))
    payments = session.scalars(
        select(PatientPayment).where(PatientPayment.claim_id == claim_id)).all()
    patient_paid = _money(sum(p.amount for p in payments))
    balance = _money(total - paid - adjustments - patient_responsibility)
    return {
        "total": total, "paid": paid, "adjustments": adjustments,
        "patient_responsibility": patient_responsibility,
        "patient_paid": patient_paid,
        "patient_due": _money(patient_responsibility - patient_paid),
        "balance": balance,
        "remittance_count": len(entries),
        "last_outcome": entries[-1].get("outcome") if entries else None,
    }


@router.post("/remittances", status_code=201)
async def ingest_remittance(request: Request, session: Session = Depends(get_db),
                            user: User = Depends(require_capability("billing.manage"))):
    """Ingest one X12 835 file (raw text body) and post it. Malformed
    file -> 422, nothing posts. Postable CLP loops update their claims
    transactionally with the batch; unpostable loops become batch
    exceptions (see the module docstring)."""
    text = (await request.body()).decode("utf-8", errors="replace")
    try:
        parsed = parse_835(text)
    except EdiError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    if not parsed["claims"]:
        raise HTTPException(
            status_code=422,
            detail="Malformed 835: the file contains no CLP claim loops")

    batch = RemittanceBatch(
        batch_id=str(uuid.uuid4()), check_number=parsed["check_number"],
        payer_name=parsed["payer"], total_amount=parsed["total_amount"],
        claim_count=0, exception_count=0, received_by=user.username)
    session.add(batch)
    session.flush()

    posted: list[dict] = []
    exceptions: list[dict] = []

    def record_exception(clp: dict, reason: str) -> None:
        row = RemittanceException(
            batch_id=batch.batch_id, claim_ref=clp.get("claim_ref") or "",
            reason=reason,
            detail={"status_code": clp.get("status_code"),
                    "charge": clp.get("charge"), "paid": clp.get("paid")})
        session.add(row)
        session.flush()
        exceptions.append({"claim_ref": row.claim_ref, "reason": reason})

    for clp in parsed["claims"]:
        claim_row = session.scalars(
            select(FhirResource).where(FhirResource.resource_type == "Claim",
                                       FhirResource.resource_id == clp["claim_ref"])
        ).first()
        if claim_row is None:
            record_exception(clp, f"unknown claim '{clp['claim_ref']}'")
            continue
        body = dict(claim_row.body or {})
        outcome = _status_outcome(session, clp["status_code"])
        if outcome is None:
            record_exception(
                clp, f"unmapped CLP02 status code '{clp['status_code']}' "
                     "(not in the published terminology/remittance-status map)")
            continue
        if body.get("status") in ("draft", "ready"):
            record_exception(
                clp, f"claim {clp['claim_ref']} is in status "
                     f"'{body.get('status')}' — a remittance can only post "
                     "against a submitted claim")
            continue
        existing = remittance_entries(body)
        if any(e.get("check_number") and e.get("check_number") == parsed["check_number"]
               for e in existing):
            record_exception(
                clp, f"duplicate remittance: check "
                     f"'{parsed['check_number']}' is already posted to "
                     f"claim {clp['claim_ref']}")
            continue

        entry = {
            "check_number": parsed["check_number"], "payer": parsed["payer"],
            "posted_at": _now_iso(), "posted_by": user.username,
            "outcome": outcome, "status_code": clp["status_code"],
            "charge": clp["charge"], "paid": clp["paid"],
            "patient_responsibility": clp["patient_responsibility"],
            "lines": clp["lines"], "adjustments": clp["adjustments"],
            "charge_mismatch": _money(clp["charge"]) != _money(
                ((body.get("total") or {}).get("value")) or 0.0),
        }
        previous_status = body.get("status")
        _append_remittance(body, entry)
        financials = claim_financials(session, clp["claim_ref"], body)
        if outcome == "denied":
            new_status = "denied"
        elif financials["balance"] <= 0:
            new_status = "paid"
        else:
            new_status = "partially-paid"
        entry["balance_after"] = financials["balance"]
        # The entry was serialised into the body above; balance_after
        # belongs on the record, so re-append happens through the same
        # list position — rewrite the valueString just written.
        infos = body["supportingInfo"]
        infos[-1] = {**infos[-1],
                     "valueString": json.dumps(entry, sort_keys=True)}
        body["status"] = new_status
        billing._append_history(body, {
            "from": previous_status, "to": new_status, "at": _now_iso(),
            "by": user.username,
            "note": f"Remittance posted (check {parsed['check_number'] or '—'})"})
        claim_row.body = body
        claim_row.status = new_status
        claim_row.version += 1
        session.flush()
        append_audit(session, actor=user.username,
                     action="billing.remittance.post",
                     entity=f"Claim/{clp['claim_ref']}",
                     payload={"check_number": parsed["check_number"],
                              "batch_id": batch.batch_id, "outcome": outcome,
                              "from_status": previous_status,
                              "to_status": new_status, "paid": clp["paid"],
                              "balance_after": financials["balance"]})
        posted.append({"claim_id": clp["claim_ref"], "outcome": outcome,
                       "from_status": previous_status, "to_status": new_status,
                       "paid": clp["paid"],
                       "patient_responsibility": clp["patient_responsibility"],
                       "balance_after": financials["balance"]})

    batch.claim_count = len(posted)
    batch.exception_count = len(exceptions)
    session.flush()
    append_audit(session, actor=user.username, action="billing.remittance.ingest",
                 entity=f"RemittanceBatch/{batch.batch_id}",
                 payload={"check_number": batch.check_number,
                          "payer": batch.payer_name,
                          "total_amount": batch.total_amount,
                          "posted": len(posted),
                          "exceptions": len(exceptions)})
    session.commit()
    return {"batch": {"id": batch.batch_id,
                      "check_number": batch.check_number,
                      "payer": batch.payer_name,
                      "total_amount": batch.total_amount,
                      "posted": len(posted), "exceptions": len(exceptions),
                      "received_by": batch.received_by,
                      "received_at": batch.created_at.isoformat()
                      if batch.created_at else None},
            "posted": posted, "exceptions": exceptions}


@router.get("/remittances")
def list_remittances(session: Session = Depends(get_db),
                     user: User = Depends(billing._view_user)):
    rows = session.scalars(
        select(RemittanceBatch).order_by(RemittanceBatch.created_at.asc(),
                                         RemittanceBatch.id.asc())).all()
    return {"items": [{"id": r.batch_id, "check_number": r.check_number,
                       "payer": r.payer_name, "total_amount": r.total_amount,
                       "posted": r.claim_count,
                       "exceptions": r.exception_count,
                       "received_by": r.received_by,
                       "received_at": r.created_at.isoformat()
                       if r.created_at else None} for r in rows],
            "total": len(rows)}


@router.get("/remittances/exceptions")
def list_remittance_exceptions(session: Session = Depends(get_db),
                               user: User = Depends(billing._view_user)):
    rows = session.scalars(
        select(RemittanceException).order_by(
            RemittanceException.created_at.asc(),
            RemittanceException.id.asc())).all()
    return {"items": [{"batch_id": r.batch_id, "claim_ref": r.claim_ref,
                       "reason": r.reason, "detail": r.detail or {},
                       "recorded_at": r.created_at.isoformat()
                       if r.created_at else None} for r in rows],
            "total": len(rows)}


# ---------------------------------------------------------------------------
# Denials workqueue
# ---------------------------------------------------------------------------

def _denial_reasons(session: Session, body: dict) -> list[dict]:
    """Every CAS adjustment recorded against the claim, with its
    denial category resolved from the published denial-categories
    config (unknown reason codes keep category 'uncategorized' and
    their verbatim code/group/amount)."""
    categories = _concept_map(session, DENIAL_CATEGORIES_KEY)
    out = []
    for entry in remittance_entries(body):
        for adj in _all_adjustments(entry):
            concept = categories.get(adj.get("reason") or "")
            out.append({
                "group": adj.get("group"), "reason": adj.get("reason"),
                "amount": adj.get("amount"), "level": adj.get("level"),
                "line_code": adj.get("line_code"),
                "category": (concept or {}).get("category", "uncategorized"),
                "display": (concept or {}).get("display"),
                "check_number": entry.get("check_number"),
            })
    return out


def _work_item(session: Session, claim_id: str) -> DenialWorkItem | None:
    return session.scalars(
        select(DenialWorkItem).where(DenialWorkItem.claim_id == claim_id)).first()


def _denial_entry(session: Session, row: FhirResource) -> dict | None:
    """The workqueue view of one claim, or None when the claim is not
    denial work: its status is `denied`, or its latest remittance
    outcome was denied, or it is partially paid carrying adjustments."""
    body = row.body or {}
    entries = remittance_entries(body)
    last_outcome = entries[-1].get("outcome") if entries else None
    status = body.get("status")
    adjustments = _denial_reasons(session, body)
    if status == "denied" or last_outcome == "denied":
        kind = "denied"
    elif status == "partially-paid" and adjustments:
        kind = "partially-paid"
    else:
        return None
    work = _work_item(session, row.resource_id)
    claim = billing._decorate(fhir._render(row))
    claim["financials"] = claim_financials(session, row.resource_id, body)
    return {
        "claim": claim, "kind": kind,
        "reasons": adjustments,
        "work": {"assignee": work.assignee if work else None,
                 "notes": work.notes if work else [],
                 "outcome": work.outcome if work else None,
                 "outcome_claim_id": work.outcome_claim_id if work else None,
                 "updated_by": work.updated_by if work else None},
    }


@router.get("/denials")
def denials_queue(include_resolved: bool = Query(default=False),
                  session: Session = Depends(get_db),
                  user: User = Depends(billing._view_user)):
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Claim")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = []
    for row in rows:
        entry = _denial_entry(session, row)
        if entry is None:
            continue
        if entry["work"].get("outcome") and not include_resolved:
            continue
        items.append(entry)
    return {"items": items, "total": len(items),
            "outcomes": list(_denial_outcomes(session))}


def _get_or_create_work_item(session: Session, claim_id: str,
                             user: User) -> DenialWorkItem:
    fhir._get_row("Claim", claim_id, session)  # 404 if unknown
    work = _work_item(session, claim_id)
    if work is None:
        work = DenialWorkItem(claim_id=claim_id, notes=[], updated_by=user.username)
        session.add(work)
        session.flush()
    return work


@router.post("/denials/{claim_id}/assign")
async def assign_denial(claim_id: str, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("billing.manage"))):
    payload = await _json_body(request)
    assignee = payload.get("assignee")
    if not isinstance(assignee, str) or not assignee.strip():
        raise HTTPException(status_code=422, detail="Assign requires 'assignee'")
    work = _get_or_create_work_item(session, claim_id, user)
    work.assignee = assignee.strip()
    work.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="billing.denial.assign",
                 entity=f"Claim/{claim_id}",
                 payload={"assignee": work.assignee})
    session.commit()
    row = fhir._get_row("Claim", claim_id, session)
    return _denial_entry(session, row)


@router.post("/denials/{claim_id}/note")
async def note_denial(claim_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("billing.manage"))):
    payload = await _json_body(request)
    note = payload.get("note")
    if not isinstance(note, str) or not note.strip():
        raise HTTPException(status_code=422, detail="Workqueue note requires 'note'")
    work = _get_or_create_work_item(session, claim_id, user)
    notes = list(work.notes or [])
    notes.append({"at": _now_iso(), "by": user.username, "text": note.strip()})
    work.notes = notes
    work.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="billing.denial.note",
                 entity=f"Claim/{claim_id}", payload={"note": note.strip()})
    session.commit()
    row = fhir._get_row("Claim", claim_id, session)
    return _denial_entry(session, row)


@router.post("/denials/{claim_id}/resolve")
async def resolve_denial(claim_id: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("billing.manage"))):
    """Record the workqueue resolution. Outcomes are the published
    denial-categories config's `outcomes` list. 'appealed' requires
    the claim to actually carry an appeal (the Phase 4A flow is the
    only way to appeal — the workqueue records, it does not fork the
    lifecycle); 'rebilled' requires naming the corrected claim the
    rebill endpoint created."""
    payload = await _json_body(request)
    outcome = payload.get("outcome")
    outcomes = _denial_outcomes(session)
    if outcome not in outcomes:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown denial outcome '{outcome}': the published "
                   f"denial-categories config allows {list(outcomes)}")
    row = fhir._get_row("Claim", claim_id, session)
    body = row.body or {}
    outcome_claim_id = payload.get("outcome_claim_id") \
        or payload.get("outcomeClaimId")
    if outcome == "appealed" and billing._appeal_count(body) < 1:
        raise HTTPException(
            status_code=422,
            detail="This claim has not been appealed: appeal it through "
                   "the claim lifecycle (POST /billing/claims/{id}/"
                   "transition to 'appealed') before resolving the "
                   "workqueue item as 'appealed'")
    if outcome == "rebilled":
        if not outcome_claim_id:
            raise HTTPException(
                status_code=422,
                detail="Resolving as 'rebilled' requires 'outcome_claim_id' "
                       "(the corrected claim created by POST "
                       "/billing/claims/{id}/rebill)")
        new_row = fhir._get_row("Claim", str(outcome_claim_id), session)
        rebill_of = None
        for ext in (new_row.body or {}).get("extension") or []:
            if isinstance(ext, dict) and ext.get("url") == REBILL_EXTENSION:
                rebill_of = ext.get("valueString")
        if rebill_of != claim_id:
            raise HTTPException(
                status_code=422,
                detail=f"Claim {outcome_claim_id} is not a rebill of "
                       f"claim {claim_id}")
    work = _get_or_create_work_item(session, claim_id, user)
    work.outcome = outcome
    work.outcome_claim_id = str(outcome_claim_id) if outcome_claim_id else None
    work.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="billing.denial.resolve",
                 entity=f"Claim/{claim_id}",
                 payload={"outcome": outcome,
                          "outcome_claim_id": work.outcome_claim_id})
    session.commit()
    return _denial_entry(session, row)


@router.post("/claims/{claim_id}/rebill", status_code=201)
def rebill_claim(claim_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("billing.manage"))):
    """Create a CORRECTED claim from a denied / partially-paid one: a
    new draft Claim copying diagnoses + lines + total, linked back to
    the original via the rebill-of extension, with a fresh status
    history and a zeroed appeal count. The original is never written
    to — it stays exactly as the payer left it."""
    row = fhir._get_row("Claim", claim_id, session)
    body = row.body or {}
    if body.get("status") not in ("denied", "partially-paid"):
        raise HTTPException(
            status_code=409,
            detail=f"Claim {claim_id} is in status '{body.get('status')}'; "
                   "only a denied or partially-paid claim can be rebilled")
    new_body = copy.deepcopy(body)
    new_id = str(uuid.uuid4())
    new_body["id"] = new_id
    new_body["status"] = "draft"
    new_body["created"] = _now_iso()
    # A corrected claim starts its own lifecycle: remittance postings
    # and the old status history stay with the ORIGINAL claim.
    new_body["supportingInfo"] = []
    billing._set_appeal_count(new_body, 0)
    billing._append_history(new_body, {
        "from": None, "to": "draft", "at": _now_iso(), "by": user.username,
        "note": f"Rebill (corrected claim) of Claim/{claim_id}"})
    extensions = [e for e in (new_body.get("extension") or [])
                  if not (isinstance(e, dict) and e.get("url") == REBILL_EXTENSION)]
    extensions.append({"url": REBILL_EXTENSION, "valueString": claim_id})
    new_body["extension"] = extensions
    created = fhir.create_resource("Claim", new_body, session, user)
    append_audit(session, actor=user.username, action="billing.claim.rebill",
                 entity=f"Claim/{new_id}",
                 payload={"rebill_of": claim_id,
                          "original_status": body.get("status"),
                          "total": (new_body.get("total") or {}).get("value")})
    session.commit()
    return billing._decorate(created)


# ---------------------------------------------------------------------------
# Patient statements + recorded payments
# ---------------------------------------------------------------------------

def _statement_user(session: Session = Depends(get_db),
                    user: User = Depends(get_current_user)) -> User:
    """The billing read gate with the patient carve-out (mirrors
    app.billing._view_user): a patient sees only their own statement;
    staff need billing.view."""
    if user.role == Role.patient:
        return user
    if not role_allowed(session, user.role, "billing.view"):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.value} lacks capability 'billing.view'")
    return user


@router.get("/statements/{patient}")
def patient_statement(patient: str, session: Session = Depends(get_db),
                      user: User = Depends(_statement_user)):
    """One patient's statement: every claim with its billed total,
    remittance paid/adjustments, patient responsibility, recorded
    payments and the amount the patient still owes. Derived entirely
    from posted records."""
    patient_ref = fhir._normalize_patient_ref(patient)
    if user.role == Role.patient and patient_ref != user.patient_ref:
        raise HTTPException(
            status_code=403,
            detail="Patient callers may only view their own statement")
    patient_id = patient_ref.split("/", 1)[1]
    fhir._get_row("Patient", patient_id, session)  # 404 if unknown
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Claim",
                                   FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    claims = []
    totals = {"total": 0.0, "paid": 0.0, "adjustments": 0.0,
              "patient_responsibility": 0.0, "patient_paid": 0.0,
              "patient_due": 0.0, "balance": 0.0}
    for row in rows:
        financials = claim_financials(session, row.resource_id, row.body or {})
        claims.append({"claim_id": row.resource_id,
                       "status": (row.body or {}).get("status"),
                       "created": (row.body or {}).get("created"),
                       **financials})
        for key in totals:
            totals[key] = _money(totals[key] + financials[key])
    return {"patient": patient_ref, "generated_at": _now_iso(),
            "claims": claims, "totals": totals,
            "disclaimer": PAYMENT_DISCLAIMER}


@router.post("/claims/{claim_id}/patient-payments", status_code=201)
async def record_patient_payment(claim_id: str, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(get_current_user)):
    """Record a patient payment against a claim's patient-
    responsibility balance. Staff need billing.manage; a patient may
    record a payment ONLY on their own claim. The method must be in
    the published payment-methods terminology, and the amount may
    not exceed what the patient still owes (422)."""
    payload = await _json_body(request)
    claim = fhir.get_resource("Claim", claim_id, session, user)  # patient-scoped 404
    if user.role != Role.patient:
        if not role_allowed(session, user.role, "billing.manage"):
            raise HTTPException(
                status_code=403,
                detail=f"Role {user.role.value} lacks capability 'billing.manage'")
    amount = payload.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) \
            or float(amount) <= 0:
        raise HTTPException(status_code=422,
                            detail="'amount' must be a positive number")
    amount = _money(amount)
    method = payload.get("method")
    methods = _concept_map(session, PAYMENT_METHODS_KEY)
    if not isinstance(method, str) or method not in methods \
            or methods[method].get("active") is False:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown payment method '{method}': not in the "
                   "published payment-methods terminology")
    row = fhir._get_row("Claim", claim_id, session)
    financials = claim_financials(session, claim_id, row.body or {})
    if amount > financials["patient_due"]:
        raise HTTPException(
            status_code=422,
            detail=f"Payment {amount:.2f} exceeds the patient's "
                   f"outstanding responsibility on claim {claim_id} "
                   f"({financials['patient_due']:.2f})")
    payment = PatientPayment(
        payment_id=str(uuid.uuid4()), claim_id=claim_id,
        patient_ref=(claim.get("patient") or {}).get("reference") or "",
        amount=amount, method=method, recorded_by=user.username)
    session.add(payment)
    session.flush()
    append_audit(session, actor=user.username,
                 action="billing.patient_payment.record",
                 entity=f"Claim/{claim_id}",
                 payload={"payment_id": payment.payment_id, "amount": amount,
                          "method": method,
                          "patient_due_after": _money(
                              financials["patient_due"] - amount)})
    session.commit()
    return {"id": payment.payment_id, "claim_id": claim_id,
            "patient": payment.patient_ref, "amount": amount,
            "method": method, "recorded_by": user.username,
            "recorded_at": payment.created_at.isoformat()
            if payment.created_at else None,
            "disclaimer": PAYMENT_DISCLAIMER}


@router.get("/claims/{claim_id}/patient-payments")
def list_patient_payments(claim_id: str, session: Session = Depends(get_db),
                          user: User = Depends(_statement_user)):
    fhir.get_resource("Claim", claim_id, session, user)  # patient-scoped 404
    rows = session.scalars(
        select(PatientPayment).where(PatientPayment.claim_id == claim_id)
        .order_by(PatientPayment.created_at.asc(),
                  PatientPayment.id.asc())).all()
    return {"items": [{"id": r.payment_id, "claim_id": r.claim_id,
                       "patient": r.patient_ref, "amount": r.amount,
                       "method": r.method, "recorded_by": r.recorded_by,
                       "recorded_at": r.created_at.isoformat()
                       if r.created_at else None} for r in rows],
            "total": len(rows),
            "disclaimer": PAYMENT_DISCLAIMER}
