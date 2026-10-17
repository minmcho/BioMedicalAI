"""Phase 4A — Billing / claims (revenue cycle).

Charge capture, a config-driven claim lifecycle, and an X12 837P-format
export, built ON the Studio customization engine like every other module:

- the fee schedule is terminology config, key ``fee-schedule`` — a code
  not in the schedule is still charged as a line, priced 0 and flagged
  ``priced: false``; a charge is NEVER silently dropped;
- the claim lifecycle is workflow config, key ``claim-lifecycle``
  (draft -> ready -> submitted -> paid / denied -> appealed ->
  submitted), seeded published v1 at startup by
  :func:`ensure_billing_defaults`; the constant below is the seed/fallback.

Claims are FHIR Claim resources stored through the generic resource layer
(app.fhir), so patient read scoping comes from the same projection rules
as the chart. Amounts on a claim are computed server-side from the fee
schedule at generation time — a caller never supplies a price.

837 export scope, stated plainly: :func:`render_837p` produces a
syntactically structured X12 837P-style EDI document (ISA/GS/ST envelope,
CLM + LX/SV1 service lines, SE/GE/IEA trailers) whose control counts and
amounts reconcile with the stored claim. It is a format-correct EXPORT
for review/testing — there is NO clearinghouse connection and no
payer submission in this codebase. (835 remittance POSTING arrived in
Phase 7 as app.revenue: a local ingest endpoint for 835 files, still
with no clearinghouse connection.)

Attribution rule for charge capture (documented in
contracts/fhir-resources.md): Procedures and Observations are charged to
an encounter when they reference it (`encounter.reference`) or, lacking a
reference, when their clinical date falls inside the encounter period.
Conditions are the patient's problem list: they become the claim's
diagnoses, each also carried as a priced "problem evaluation" line.
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .configstore import (load_workflow_machine, published_config,
                          role_allowed, transition_role_allowed,
                          workflow_payload)
from .models import (CodingRecord, ConfigStatus, FhirResource, Role,
                     StudioConfig, User)
from .security import get_current_user, get_db, require_capability

router = APIRouter(prefix="/billing", tags=["billing"])

FEE_SCHEDULE_KEY = "fee-schedule"
CLAIM_WORKFLOW_KEY = "claim-lifecycle"
CURRENCY = "USD"
APPEAL_COUNT_EXTENSION = "urn:opencare:extension/appeal-count"
CHARGE_SOURCE_EXTENSION = "urn:opencare:extension/charge-source"
PRICED_EXTENSION = "urn:opencare:extension/priced"
STATUS_HISTORY_CATEGORY = "urn:opencare:claim-info"

# States from which an 837 export may be produced (the claim has been —
# or is being treated as — submitted).
EXPORTABLE_STATES = ("submitted", "paid", "denied", "appealed")

# Fallback copy of the claim lifecycle — the live machine is the published
# workflow config (seeded v1 below with exactly these transitions); this
# constant is used only if that config is disabled.
DEFAULT_CLAIM_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"ready"},
    "ready": {"submitted"},
    "submitted": {"paid", "denied"},
    "denied": {"appealed"},
    "appealed": {"submitted"},
    "paid": set(),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def default_fee_schedule_payload() -> dict:
    """Synthetic demo price list (USD). Codes are the ones the seed data
    and lab catalog actually use (CPT procedures, LOINC observations,
    SNOMED problem evaluations) plus two common office-visit CPTs."""
    def fee(system, code, display, price):
        return {"code": code, "display": display, "active": True,
                "system": system, "unitPrice": price}

    cpt = "http://www.ama-assn.org/go/cpt"
    loinc = "http://loinc.org"
    sct = "http://snomed.info/sct"
    concepts = [
        # Procedures (CPT)
        fee(cpt, "99213", "Office visit, established patient (low)", 120.00),
        fee(cpt, "99214", "Office visit, established patient (moderate)", 180.00),
        fee(cpt, "93005", "Electrocardiogram", 45.00),
        fee(cpt, "71046", "Chest X-ray, 2 views", 85.00),
        fee(cpt, "20610", "Arthrocentesis, knee", 210.00),
        fee(cpt, "90791", "Psychiatric diagnostic evaluation", 190.00),
        fee(cpt, "94640", "Inhalation therapy administration", 35.00),
        # Observations / laboratory (LOINC)
        fee(loinc, "4548-4", "Hemoglobin A1c", 28.00),
        fee(loinc, "2345-7", "Glucose", 12.00),
        fee(loinc, "13457-7", "LDL cholesterol", 22.00),
        fee(loinc, "14682-9", "Creatinine", 14.00),
        fee(loinc, "2823-3", "Potassium", 11.00),
        fee(loinc, "718-7", "Hemoglobin", 10.00),
        fee(loinc, "58410-2", "Complete blood count panel", 35.00),
        fee(loinc, "51990-0", "Basic metabolic panel", 32.00),
        # Problem evaluations (SNOMED CT condition codes)
        fee(sct, "44054006", "Type 2 diabetes mellitus — problem evaluation", 55.00),
        fee(sct, "38341003", "Hypertensive disorder — problem evaluation", 45.00),
        fee(sct, "195967001", "Asthma — problem evaluation", 40.00),
        fee(sct, "55822004", "Hyperlipidemia — problem evaluation", 30.00),
    ]
    return {"name": "OpenCare fee schedule (synthetic demo prices, USD)",
            "concepts": concepts}


def default_claim_workflow_payload() -> dict:
    states = sorted(DEFAULT_CLAIM_TRANSITIONS.keys())
    transitions = [
        {"from": src, "to": dst}
        for src in sorted(DEFAULT_CLAIM_TRANSITIONS)
        for dst in sorted(DEFAULT_CLAIM_TRANSITIONS[src])
    ]
    # exportableStates + maxAppeals ride in the workflow payload (they
    # used to be the EXPORTABLE_STATES constant and a literal `>= 1`
    # appeal check; both remain as fallbacks for payloads without them).
    return {"states": states, "transitions": transitions,
            "exportableStates": list(EXPORTABLE_STATES), "maxAppeals": 1}


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


def ensure_billing_defaults(session: Session) -> None:
    """Seed the fee schedule + claim lifecycle as published Studio config
    v1 (idempotent), exactly like the lab/ADT config seeds."""
    _seed_published(session, "terminology", FEE_SCHEDULE_KEY,
                    default_fee_schedule_payload())
    _seed_published(session, "workflow", CLAIM_WORKFLOW_KEY,
                    default_claim_workflow_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Fee schedule + lifecycle machine (config accessors)
# ---------------------------------------------------------------------------

def _fee_index(session: Session) -> dict:
    """code -> fee concept, and (system, code) -> fee concept."""
    cfg = published_config(session, "terminology", FEE_SCHEDULE_KEY)
    by_code: dict[str, dict] = {}
    by_system_code: dict[tuple[str, str], dict] = {}
    if cfg is not None:
        for concept in (cfg.payload or {}).get("concepts") or []:
            if not isinstance(concept, dict) or not concept.get("code"):
                continue
            if concept.get("active") is False:
                continue
            by_code.setdefault(concept["code"], concept)
            if concept.get("system"):
                by_system_code[(concept["system"], concept["code"])] = concept
    return {"by_code": by_code, "by_system_code": by_system_code}


def price_for(index: dict, system: str | None, code: str | None) -> float | None:
    """Unit price for a coded charge, or None when the code is not in the
    published fee schedule (the caller then prices the line 0 and flags it
    unpriced — the charge is never dropped)."""
    if not code:
        return None
    concept = None
    if system:
        concept = index["by_system_code"].get((system, code))
    if concept is None:
        concept = index["by_code"].get(code)
    if concept is None:
        return None
    price = concept.get("unitPrice")
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    return float(price)


def _claim_machine(session: Session):
    """(transitions, known states, per-transition roles) from the
    published claim-lifecycle config; built-in map as fallback."""
    return load_workflow_machine(session, CLAIM_WORKFLOW_KEY,
                                 DEFAULT_CLAIM_TRANSITIONS)


def _exportable_states(session: Session) -> tuple:
    """States from which an 837 export may be produced, from the claim
    workflow payload's `exportableStates` (fallback: the built-in set)."""
    values = (workflow_payload(session, CLAIM_WORKFLOW_KEY) or {}).get(
        "exportableStates")
    if isinstance(values, list) and values and all(
            isinstance(v, str) and v for v in values):
        return tuple(values)
    return EXPORTABLE_STATES


def _max_appeals(session: Session) -> int:
    """How many times a claim may be appealed, from the claim workflow
    payload's `maxAppeals` (fallback: 1 — the original one-appeal rule)."""
    value = (workflow_payload(session, CLAIM_WORKFLOW_KEY) or {}).get("maxAppeals")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 1


def _require_coding(session: Session) -> bool:
    """True when the published claim-lifecycle payload sets
    `requireCoding` (Phase 7): claim generation then refuses an
    encounter that has no HIM coding record (app.revenue). Absent or
    false — the seeded default — preserves the pre-Phase-7 behaviour
    exactly: coding is consumed when present, never required."""
    value = (workflow_payload(session, CLAIM_WORKFLOW_KEY) or {}).get("requireCoding")
    return value is True


def coding_record_for(session: Session, encounter_id: str) -> CodingRecord | None:
    """The HIM coding record for one encounter, or None (Phase 7)."""
    return session.scalars(
        select(CodingRecord).where(
            CodingRecord.encounter_id == str(encounter_id))).first()


# ---------------------------------------------------------------------------
# Claim body helpers (appeals + status history ride in FHIR-plausible
# places: an extension for the appeal counter, supportingInfo entries for
# the transition history, so the stored Claim stays FHIR-shaped JSON)
# ---------------------------------------------------------------------------

def _appeal_count(body: dict) -> int:
    for ext in body.get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == APPEAL_COUNT_EXTENSION:
            value = ext.get("valueInteger")
            return int(value) if isinstance(value, int) else 0
    return 0


def _set_appeal_count(body: dict, count: int) -> None:
    extensions = [e for e in (body.get("extension") or [])
                  if not (isinstance(e, dict) and e.get("url") == APPEAL_COUNT_EXTENSION)]
    extensions.append({"url": APPEAL_COUNT_EXTENSION, "valueInteger": count})
    body["extension"] = extensions


def claim_history(body: dict) -> list[dict]:
    history = []
    for info in body.get("supportingInfo") or []:
        if not isinstance(info, dict):
            continue
        category = ((info.get("category") or {}).get("coding") or [{}])[0]
        if category.get("system") != STATUS_HISTORY_CATEGORY:
            continue
        if category.get("code") != "status-history":
            continue
        try:
            history.append(json.loads(info.get("valueString") or "{}"))
        except (TypeError, ValueError):
            continue
    return history


def _append_history(body: dict, entry: dict) -> None:
    infos = list(body.get("supportingInfo") or [])
    infos.append({
        "sequence": len(infos) + 1,
        "category": {"coding": [{"system": STATUS_HISTORY_CATEGORY,
                                 "code": "status-history",
                                 "display": "Claim status history"}]},
        "valueString": json.dumps(entry, sort_keys=True),
    })
    body["supportingInfo"] = infos


def _decorate(claim: dict) -> dict:
    """Render helper: expose the parsed history + appeal count at top
    level of API responses (the stored body keeps them in extension /
    supportingInfo)."""
    out = dict(claim)
    out["appealCount"] = _appeal_count(claim)
    out["statusHistory"] = claim_history(claim)
    return out


# ---------------------------------------------------------------------------
# Charge capture
# ---------------------------------------------------------------------------

def _first_coding(body: dict, field: str = "code") -> tuple[str | None, str | None, str | None]:
    concept = body.get(field) or {}
    coding = concept.get("coding") or []
    if coding and isinstance(coding[0], dict):
        return coding[0].get("system"), coding[0].get("code"), coding[0].get("display")
    return None, None, concept.get("text") if isinstance(concept, dict) else None


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


def _clinical_date(resource_type: str, body: dict) -> datetime | None:
    if resource_type == "Observation":
        return _parse_dt(body.get("effectiveDateTime") or body.get("issued"))
    if resource_type == "Procedure":
        period = body.get("performedPeriod") or {}
        return _parse_dt(body.get("performedDateTime") or period.get("start"))
    return None


def _encounter_period(encounter_body: dict) -> tuple[datetime | None, datetime | None]:
    period = encounter_body.get("period") or {}
    return _parse_dt(period.get("start")), _parse_dt(period.get("end"))


def _attributed(encounter_id: str, encounter_body: dict, resource_type: str,
                body: dict) -> bool:
    """A Procedure/Observation belongs to the billed encounter when it
    references the encounter, or — carrying no encounter reference — when
    its clinical date falls inside the encounter period."""
    ref = (body.get("encounter") or {}).get("reference")
    if ref:
        return ref == f"Encounter/{encounter_id}"
    start, end = _encounter_period(encounter_body)
    when = _clinical_date(resource_type, body)
    if start is None or when is None:
        return False
    if end is not None:
        return start <= when <= end
    return when.date() == start.date()


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


@router.post("/claims/generate", status_code=201)
async def generate_claim(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability("billing.manage"))):
    """Charge capture: build a draft Claim from one Encounter plus the
    patient's Conditions (diagnoses / problem-evaluation lines) and the
    encounter-attributed Procedures and Observations. Prices come from
    the published fee schedule; unknown codes are priced 0 and flagged
    unpriced, never dropped. Totals are computed here, server-side."""
    payload = await _json_body(request)
    encounter_id = payload.get("encounter_id") or payload.get("encounterId")
    if not encounter_id and isinstance(payload.get("encounter"), dict):
        reference = payload["encounter"].get("reference") or ""
        encounter_id = reference.split("/", 1)[1] if "/" in reference else reference
    if not encounter_id:
        raise HTTPException(status_code=422,
                            detail="Claim generation requires 'encounter_id'")
    encounter = fhir._get_row("Encounter", str(encounter_id), session)
    encounter_body = encounter.body or {}
    patient_ref = (encounter_body.get("subject") or {}).get("reference")
    if not patient_ref:
        raise HTTPException(status_code=422,
                            detail=f"Encounter/{encounter_id} has no subject patient")
    patient_id = patient_ref.split("/", 1)[1]
    fhir._get_row("Patient", patient_id, session)  # 404 if the patient is unknown

    # Phase 7 (HIM coding): when a coding record exists for the
    # encounter it is the coded truth for the claim — its diagnoses
    # form the diagnosis section and its procedures become claim
    # lines. When the published claim config sets `requireCoding`,
    # an uncoded encounter cannot generate a claim at all (409).
    coding = coding_record_for(session, str(encounter_id))
    if coding is None and _require_coding(session):
        raise HTTPException(
            status_code=409,
            detail=(f"Encounter/{encounter_id} has not been coded: the "
                    "published claim-lifecycle config sets "
                    "'requireCoding', so a HIM coding record "
                    "(POST /billing/coding/{encounter_id}) must exist "
                    "before a claim can be generated"))

    fees = _fee_index(session)

    def rows_of(resource_type: str) -> list[FhirResource]:
        return list(session.scalars(
            select(FhirResource)
            .where(FhirResource.resource_type == resource_type,
                   FhirResource.patient_ref == patient_ref)
            .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all())

    lines: list[dict] = []
    diagnoses: list[dict] = []

    def add_line(source_type: str, row: FhirResource) -> None:
        system, code, display = _first_coding(row.body or {},
                                              "medicationCodeableConcept"
                                              if source_type == "MedicationRequest" else "code")
        price = price_for(fees, system, code)
        priced = price is not None
        unit_price = price if priced else 0.0
        sequence = len(lines) + 1
        lines.append({
            "sequence": sequence,
            "productOrService": {
                "coding": [{"system": system, "code": code, "display": display}],
                "text": display,
            },
            "quantity": {"value": 1},
            "unitPrice": {"value": unit_price, "currency": CURRENCY},
            "net": {"value": round(unit_price, 2), "currency": CURRENCY},
            "extension": [
                {"url": CHARGE_SOURCE_EXTENSION,
                 "valueString": f"{source_type}/{row.resource_id}"},
                {"url": PRICED_EXTENSION, "valueBoolean": priced},
            ],
        })

    coded_procedure_codes: set[str] = set()
    if coding is not None:
        # The coding record's diagnoses ARE the claim's diagnosis
        # section (the problem list still charges its evaluation
        # lines below, as before).
        for entry in coding.diagnoses or []:
            if not isinstance(entry, dict) or not entry.get("code"):
                continue
            diagnoses.append({
                "sequence": len(diagnoses) + 1,
                "diagnosisCodeableConcept": {
                    "coding": [{"system": entry.get("system"),
                                "code": entry.get("code"),
                                "display": entry.get("display")}],
                    "text": entry.get("display"),
                },
            })
        coded_procedure_codes = {
            entry.get("code") for entry in (coding.procedures or [])
            if isinstance(entry, dict) and entry.get("code")}

    for row in rows_of("Condition"):
        system, code, display = _first_coding(row.body or {})
        if coding is None:
            diagnoses.append({
                "sequence": len(diagnoses) + 1,
                "diagnosisCodeableConcept": {
                    "coding": [{"system": system, "code": code, "display": display}],
                    "text": display,
                },
            })
        add_line("Condition", row)
    for resource_type in ("Procedure", "Observation"):
        for row in rows_of(resource_type):
            if not _attributed(str(encounter_id), encounter_body, resource_type, row.body or {}):
                continue
            if resource_type == "Procedure" and coding is not None:
                # A chart procedure the coder also coded is charged
                # ONCE — as the coded line below (the coded record is
                # the HIM truth for the encounter). An uncoded chart
                # procedure still charges: a charge is never dropped.
                _, chart_code, _ = _first_coding(row.body or {})
                if chart_code in coded_procedure_codes:
                    continue
            add_line(resource_type, row)
    if coding is not None:
        for entry in coding.procedures or []:
            if not isinstance(entry, dict) or not entry.get("code"):
                continue
            system_c, code_c, display_c = entry.get("system"), entry.get("code"), \
                entry.get("display")
            price_c = price_for(fees, system_c, code_c)
            priced_c = price_c is not None
            unit_price_c = price_c if priced_c else 0.0
            lines.append({
                "sequence": len(lines) + 1,
                "productOrService": {
                    "coding": [{"system": system_c, "code": code_c,
                                "display": display_c}],
                    "text": display_c,
                },
                "quantity": {"value": 1},
                "unitPrice": {"value": unit_price_c, "currency": CURRENCY},
                "net": {"value": round(unit_price_c, 2), "currency": CURRENCY},
                "extension": [
                    {"url": CHARGE_SOURCE_EXTENSION,
                     "valueString": f"CodingRecord/{encounter_id}"},
                    {"url": PRICED_EXTENSION, "valueBoolean": priced_c},
                ],
            })

    if not lines:
        raise HTTPException(
            status_code=422,
            detail=(f"No billable items found for Encounter/{encounter_id}: no "
                    "conditions on the patient's problem list and no procedures "
                    "or observations attributed to the encounter"))

    total = round(sum(line["net"]["value"] for line in lines), 2)
    claim_body = {
        "resourceType": "Claim",
        "id": str(uuid.uuid4()),
        "status": "draft",
        "type": {"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/claim-type",
            "code": "professional", "display": "Professional"}]},
        "use": "claim",
        "patient": {"reference": patient_ref},
        "encounter": [{"reference": f"Encounter/{encounter_id}"}],
        "provider": {"reference": "Organization/opencare",
                     "display": "OpenCare Clinic"},
        "insurer": {"display": "Synthetic Payer"},
        "created": _now_iso(),
        "diagnosis": diagnoses,
        "item": lines,
        "total": {"value": total, "currency": CURRENCY},
    }
    _set_appeal_count(claim_body, 0)
    _append_history(claim_body, {"from": None, "to": "draft", "at": _now_iso(),
                                 "by": user.username, "note": "Claim generated"})
    claim = fhir.create_resource("Claim", claim_body, session, user)
    append_audit(session, actor=user.username, action="billing.claim.generate",
                 entity=f"Claim/{claim['id']}",
                 payload={"encounter_id": str(encounter_id), "patient": patient_ref,
                          "line_count": len(lines), "total": total,
                          "unpriced_lines": sum(
                              1 for line in lines
                              if not line["extension"][1]["valueBoolean"])})
    session.commit()
    return _decorate(claim)


# ---------------------------------------------------------------------------
# Claim reads (capability-gated; a patient sees only their OWN claims)
# ---------------------------------------------------------------------------

def _view_user(session: Session = Depends(get_db),
               user: User = Depends(get_current_user)) -> User:
    """billing.view gate with the patient carve-out: a patient caller may
    view (only ever their own claims — enforced by the read paths); staff
    need the billing.view capability (defaults admin/configurator/
    clinician; viewer is denied unless Studio permissions config grants
    it)."""
    if user.role == Role.patient:
        return user
    if not role_allowed(session, user.role, "billing.view"):
        raise HTTPException(
            status_code=403,
            detail=f"Role {user.role.value} lacks capability 'billing.view'")
    return user


@router.get("/claims")
def list_claims(status: str | None = Query(default=None),
                patient: str | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(_view_user)):
    if user.role == Role.patient:
        if patient and fhir._normalize_patient_ref(patient) != user.patient_ref:
            raise HTTPException(status_code=403,
                                detail="Patient callers may only view their own claims")
        patient = user.patient_ref
    stmt = select(FhirResource).where(FhirResource.resource_type == "Claim")
    if status:
        stmt = stmt.where(FhirResource.status == status)
    if patient:
        stmt = stmt.where(FhirResource.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.order_by(FhirResource.created_at.asc(),
                                         FhirResource.id.asc())).all()
    bundle = fhir._bundle(rows)
    bundle["entry"] = [{"resource": _decorate(e["resource"])} for e in bundle["entry"]]
    return bundle


@router.get("/claims/{claim_id}")
def get_claim(claim_id: str, session: Session = Depends(get_db),
              user: User = Depends(_view_user)):
    # fhir.get_resource applies patient non-disclosure scoping (404 for
    # another patient's claim).
    return _decorate(fhir.get_resource("Claim", claim_id, session, user))


# ---------------------------------------------------------------------------
# Claim lifecycle (config-driven) + one-time appeal
# ---------------------------------------------------------------------------

@router.post("/claims/{claim_id}/transition")
async def transition_claim(claim_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("billing.manage"))):
    """Advance a claim through the published claim-lifecycle workflow
    config. Illegal transitions -> 409; unknown target state -> 422.
    Moving to 'appealed' is the appeal: it is legal only from 'denied',
    requires a note, and may happen at most `maxAppeals` times per claim
    (a workflow-config number, default 1) — a further appeal is refused
    with 409 and the earlier appeals stay in the history."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    row = fhir._get_row("Claim", claim_id, session)
    body = dict(row.body or {})
    current = body.get("status")
    transitions, known_states, roles = _claim_machine(session)
    if target not in known_states:
        raise HTTPException(status_code=422, detail=f"Unknown claim status: {target}")
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal claim transition '{current}' -> '{target}'")
    if not transition_role_allowed(roles, current, target, user.role.value):
        raise HTTPException(
            status_code=403,
            detail=(f"Role {user.role.value} may not perform the claim "
                    f"transition '{current}' -> '{target}': the published "
                    "workflow config restricts it to "
                    f"{', '.join(roles.get((current, target), []))}"))
    note = payload.get("note")
    if target == "appealed":
        if not isinstance(note, str) or not note.strip():
            raise HTTPException(
                status_code=422,
                detail="Appealing a denied claim requires a 'note' explaining the appeal")
        max_appeals = _max_appeals(session)
        if _appeal_count(body) >= max_appeals:
            if max_appeals == 1:
                detail = (f"Claim {claim_id} has already been appealed once; "
                          "a second appeal is not permitted")
            else:
                detail = (f"Claim {claim_id} has already been appealed "
                          f"{_appeal_count(body)} time(s); the published "
                          f"workflow config allows at most {max_appeals}")
            raise HTTPException(status_code=409, detail=detail)
        _set_appeal_count(body, _appeal_count(body) + 1)
    body["status"] = target
    _append_history(body, {"from": current, "to": target, "at": _now_iso(),
                           "by": user.username,
                           "note": note if isinstance(note, str) else None})
    row.body = body
    row.status = target
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="billing.claim.transition",
                 entity=f"Claim/{claim_id}",
                 payload={"from_status": current, "to_status": target,
                          "note": note if isinstance(note, str) else None,
                          "version": row.version})
    session.commit()
    return _decorate(fhir._render(row))


# ---------------------------------------------------------------------------
# X12 837P export
# ---------------------------------------------------------------------------

def _edi_date(raw: str | None) -> tuple[str, str, str]:
    """(YYYYMMDD, YYMMDD, HHMM) from an ISO date/datetime; 'now' fallback."""
    parsed = _parse_dt(raw) if raw else None
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    return parsed.strftime("%Y%m%d"), parsed.strftime("%y%m%d"), parsed.strftime("%H%M")


def _control_number(claim_id: str) -> int:
    digest = hashlib.sha256(claim_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 1_000_000_000


def render_837p(claim: dict, patient: dict) -> str:
    """Render a syntactically structured X12 837P-style document from a
    stored claim. Segment terminators are '~' + newline. The SE count,
    CLM total and SV1 amounts reconcile by construction: SV1 amounts are
    the claim's line nets and CLM02 is the claim total (their sum)."""
    claim_id = str(claim.get("id") or "")
    created = claim.get("created")
    ymd, ymd2, hm = _edi_date(created if isinstance(created, str) else None)
    isa_ctrl = _control_number(claim_id)

    name = (patient.get("name") or [{}])[0]
    family = (name.get("family") or "UNKNOWN").upper()
    given = ((name.get("given") or ["UNKNOWN"])[0]).upper()
    birth = _edi_date(patient.get("birthDate"))[0] if patient.get("birthDate") else ""
    gender = {"female": "F", "male": "M"}.get(str(patient.get("gender") or "").lower(), "U")

    total = float(((claim.get("total") or {}).get("value")) or 0.0)
    items = claim.get("item") or []
    diagnosis_codes = []
    for diagnosis in claim.get("diagnosis") or []:
        coding = ((diagnosis.get("diagnosisCodeableConcept") or {}).get("coding") or [{}])[0]
        if coding.get("code"):
            diagnosis_codes.append(str(coding["code"]))

    segments: list[str] = [
        f"ISA*00*          *00*          *ZZ*OPENCARE       *ZZ*SYNTHPAYER     "
        f"*{ymd2}*{hm}*^*00501*{isa_ctrl:09d}*0*T*:",
        f"GS*HC*OPENCARE*SYNTHPAYER*{ymd}*{hm}*1*X*005010X222A1",
        "ST*837*0001*005010X222A1",
        f"BHT*0019*00*{claim_id[:18]}*{ymd}*{hm}*CH",
        "NM1*41*2*OPENCARE CLINIC*****46*OPENCARE-ETIN",
        "NM1*40*2*SYNTHETIC PAYER*****46*SYNTH-PAYER",
        "HL*1**20*1",
        "NM1*85*2*OPENCARE CLINIC*****XX*1999999992",
        "HL*2*1*22*0",
        "SBR*P*18*SYNTHGRP*****MB",
        f"NM1*IL*1*{family}*{given}****MI*SYNTH-MEMBER-001",
    ]
    if birth:
        segments.append(f"DMG*D8*{birth}*{gender}")
    segments.append(f"CLM*{claim_id}*{total:.2f}***11:B:1*Y*A*Y*I")
    if diagnosis_codes:
        hi = "*".join(
            f"{'ABK' if i == 0 else 'ABF'}:{code}"
            for i, code in enumerate(diagnosis_codes))
        segments.append(f"HI*{hi}")
    pointers = ":".join(str(i + 1) for i in range(len(diagnosis_codes))) or None
    for i, item in enumerate(items):
        sequence = item.get("sequence") or i + 1
        coding = ((item.get("productOrService") or {}).get("coding") or [{}])[0]
        code = coding.get("code") or "UNKNOWN"
        net = float(((item.get("net") or {}).get("value")) or 0.0)
        qty = float(((item.get("quantity") or {}).get("value")) or 1)
        qty_text = str(int(qty)) if qty == int(qty) else str(qty)
        segments.append(f"LX*{sequence}")
        sv1 = f"SV1*HC:{code}*{net:.2f}*UN*{qty_text}"
        if pointers:
            sv1 += f"***{pointers}"
        segments.append(sv1)
    # SE count covers ST..SE inclusive: segments after GS plus the SE itself.
    se_count = len(segments) - 2 + 1
    segments.append(f"SE*{se_count}*0001")
    segments.append("GE*1*1")
    segments.append(f"IEA*1*{isa_ctrl:09d}")
    return "".join(f"{segment}~\n" for segment in segments)


@router.get("/claims/{claim_id}/export/837")
def export_837(claim_id: str, session: Session = Depends(get_db),
               user: User = Depends(_view_user)):
    """Download the claim as an X12 837P-style EDI text file. Only a claim
    that has reached 'submitted' (or beyond) may be exported — exporting a
    draft would misrepresent it as transmitted. This is a file export
    only; nothing is transmitted to any clearinghouse or payer."""
    claim = fhir.get_resource("Claim", claim_id, session, user)
    status = claim.get("status")
    if status not in _exportable_states(session):
        raise HTTPException(
            status_code=409,
            detail=(f"Claim {claim_id} is in status '{status}'; an 837 export "
                    "requires a submitted claim"))
    patient_ref = (claim.get("patient") or {}).get("reference") or ""
    patient_id = patient_ref.split("/", 1)[1] if "/" in patient_ref else ""
    patient_row = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Patient",
                                   FhirResource.resource_id == patient_id)).first()
    if patient_row is None:
        raise HTTPException(status_code=422,
                            detail=f"Claim references unknown patient '{patient_ref}'")
    text = render_837p(claim, fhir._render(patient_row))
    append_audit(session, actor=user.username, action="billing.claim.export837",
                 entity=f"Claim/{claim_id}",
                 payload={"status": status, "total": (claim.get("total") or {}).get("value")})
    session.commit()
    return PlainTextResponse(
        text, media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="claim-{claim_id}-837.txt"'})
