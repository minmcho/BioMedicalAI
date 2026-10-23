"""Phase 12 — Care-continuum operations (PHASE10_ROADMAP Track D).

ER -> inpatient -> discharge -> aftercare as one journey, outpatient
as the parallel front door, a Capacity Command Center over all of it,
and safety & quality operations (Armstrong Institute pattern) beside
it. Everything here is built ON the existing modules, not beside
them: the ED board's encounters (app.boards), beds/ADT/census
(app.inpatient), appointments (app.fhir), referrals (app.access),
message threads (app.engagement), the rules engine (app.rules) and
the compliance register (app.compliance).

Design rules, same as every phase:

- **Configure, don't fork.** Every threshold, timer, template and
  taxonomy a site would tune is the published `terminology/
  continuum-ops` config (seeded below): ESI target minutes, fast-
  track levels, activation pathways + milestones, diversion states,
  EMTALA required-element lists, inbox SLAs, clinic session templates
  + overbooking limits, no-show thresholds, the readmission-risk
  factor weights, the post-discharge call window, harm taxonomy.
- **Derived beats stored.** Boarding, queues, readiness, EVS
  turnaround, care gaps and readmission risk are COMPUTED from live
  module data (encounters, beds, appointments, lab state) on every
  read. Stored rows exist only where an act must be recorded: an
  activation started, a callback made, an assignment given, a
  transfer decided, a safety report filed.
- **Deterministic, labelled.** Predicted census is an arithmetic
  projection from expected discharges + pending bed requests;
  readmission risk is a weighted sum of config-named factors;
  no-show risk is the patient's own historical miss rate. No ML is
  claimed anywhere; every computed score names its basis in the
  response.
- **EMTALA is a gate, not a label.** The transfer-out endpoint
  refuses to close an ER record until the medical-screening-exam
  form and the transfer-certification form (both governed Studio
  forms whose required elements are the config's lists) are on file
  for the encounter.
"""
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import (access, boards, documentation, engagement, fhir, inpatient,
               org_hierarchy)
from .audit import append_audit
from .configstore import list_config_versions, published_config, role_allowed
from .models import (AdtEvent, AftercareCall, ComplianceRequirement,
                     EdActivation, EvsTask, FacilityOpsState, FhirResource,
                     LabCriticalNotification, NurseAssignment, ResultCallback,
                     SafetyEvent, SafetyEventMessage, TransferRequest,
                     TransportRequest, User, WaitlistEntry)
from .rules import evaluate_rules
from .security import get_current_user, get_db, require_capability

router = APIRouter(prefix="/continuum", tags=["continuum"])

OPS_KEY = "continuum-ops"
READ = "continuum.read"
WORK = "continuum.work"

HAH_CLASS_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
HAH_ELIGIBLE_EXTENSION = "urn:opencare:extension/hah-eligible"
HOME_HEALTH_EXTENSION = "urn:opencare:extension/home-health"
SESSION_EXTENSION = "urn:opencare:extension/clinic-session"
ROOMING_EXTENSION = "urn:opencare:extension/rooming"
LWBS_EXTENSION = "urn:opencare:extension/lwbs"
TRANSFER_OUT_EXTENSION = "urn:opencare:extension/transfer-out"
REPORT_BACK_EXTENSION = "urn:opencare:extension/report-back"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_dt(raw) -> datetime | None:
    return boards._parse_dt(raw)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


def _require_str(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}' is required and must be a non-empty string")
    return value.strip()


# ---------------------------------------------------------------------------
# Published operations config (terminology/continuum-ops)
# ---------------------------------------------------------------------------

def default_ops_payload() -> dict:
    """Synthetic care-continuum defaults. Timers, thresholds,
    templates and taxonomies are this config object, not code — a
    site retunes its ER targets, its call window or its risk weights
    by publishing a new version (standing rule: configure, don't
    fork)."""
    return {
        "name": "Care-continuum operations (synthetic defaults)",
        # The document doubles as the facility's diversion-state value
        # set, which also keeps it a valid `terminology` config.
        "concepts": [
            {"code": "open", "display": "Open — accepting all arrivals",
             "active": True},
            {"code": "diversion-ed",
             "display": "Diverting ED arrivals", "active": True},
            {"code": "diversion-trauma",
             "display": "Diverting trauma arrivals", "active": True},
            {"code": "internal-disaster",
             "display": "Internal disaster — facility-wide diversion",
             "active": True}],
        # ER: door-to-provider target minutes per ESI level.
        "esiTargetsMinutes": {"1": 0, "2": 10, "3": 30, "4": 60, "5": 120},
        "fastTrackEsiLevels": [4, 5],
        "activationPathways": [
            {"code": "stemi", "display": "STEMI activation",
             "targetMinutes": 90,
             "milestones": [
                 {"code": "ecg", "display": "ECG performed", "targetMinutes": 10},
                 {"code": "cath-lab", "display": "Cath lab activated", "targetMinutes": 30},
                 {"code": "reperfusion", "display": "Reperfusion (balloon/device)", "targetMinutes": 90}]},
            {"code": "stroke", "display": "Stroke activation",
             "targetMinutes": 60,
             "milestones": [
                 {"code": "ct-head", "display": "CT head completed", "targetMinutes": 25},
                 {"code": "needle", "display": "Thrombolytic needle time", "targetMinutes": 60}]},
            {"code": "trauma", "display": "Trauma activation",
             "targetMinutes": 30,
             "milestones": [
                 {"code": "team-assembled", "display": "Trauma team assembled", "targetMinutes": 15},
                 {"code": "imaging", "display": "Trauma imaging completed", "targetMinutes": 30}]},
        ],
        "diversionStates": [
            {"code": "open", "display": "Open — accepting all arrivals"},
            {"code": "diversion-ed", "display": "ED diversion — divert ambulances"},
            {"code": "diversion-trauma", "display": "Trauma diversion"},
            {"code": "internal-disaster", "display": "Internal disaster"},
        ],
        "surgeLevels": [
            {"code": "normal", "display": "Normal"},
            {"code": "elevated", "display": "Elevated"},
            {"code": "surge", "display": "Surge"},
            {"code": "critical", "display": "Critical"},
        ],
        # EMTALA: the required documentation elements ARE the item
        # lists of the governed forms seeded from this config; the
        # transfer-out gate reads them from here.
        "emtala": {
            "mseFormKey": "emtala-mse",
            "transferFormKey": "emtala-transfer-certification",
            "mseRequiredElements": [
                "presenting-complaint", "vital-signs",
                "screening-exam-performed", "exam-findings",
                "stabilising-treatment", "examiner"],
            "transferRequiredElements": [
                "risks-benefits-discussed", "patient-consent",
                "receiving-facility", "receiving-physician",
                "transport-mode", "medical-records-sent"],
        },
        "inboxSlaMinutes": {"task": 480, "message": 240, "result": 120,
                            "callback": 240},
        "clinicSessions": [
            {"code": "am-clinic", "display": "Morning clinic",
             "startTime": "09:00", "slotMinutes": 20, "capacity": 12,
             "overbookLimit": 2},
            {"code": "pm-clinic", "display": "Afternoon clinic",
             "startTime": "13:00", "slotMinutes": 20, "capacity": 12,
             "overbookLimit": 2},
            {"code": "fast-track-clinic", "display": "Fast-track clinic",
             "startTime": "10:00", "slotMinutes": 15, "capacity": 8,
             "overbookLimit": 0},
        ],
        "noShowRisk": {"minHistory": 3, "highRiskMissRate": 0.34},
        "referralStaleDays": 14,
        "readmissionRisk": {
            "windowDays": 30, "flagThreshold": 5,
            "facilityDispositions": ["snf", "rehab", "ltach"],
            "factors": {"age65Plus": 1, "age80Plus": 2, "losDays7Plus": 2,
                        "priorAdmissions90d": 2, "conditions5Plus": 2,
                        "meds10Plus": 1, "dischargedToFacility": 1},
            "basis": ("deterministic weighted sum of the configured "
                      "factors over chart data — a triage aid for the "
                      "aftercare team, not a validated prediction model"),
        },
        "callQueue": {"windowStartHours": 48, "windowEndHours": 72,
                      "outcomes": ["reached-well", "reached-concern",
                                   "reached-needs-visit", "no-answer",
                                   "wrong-number"]},
        # requireMedRec (depth pass, Workstream 2): when true, the
        # readiness board lists "medication-reconciliation" as missing
        # until a discharge-med-rec form is on file. Ships FALSE —
        # discharge behaviour is unchanged until a site flips it.
        "dischargeGuards": {"followUpRequired": False,
                            "requireMedRec": False},
        "nurseLoad": {"maxAcuityPerNurse": 12},
        "acuityLevels": [1, 2, 3, 4, 5],
        "harmTaxonomy": [
            {"code": "near-miss", "display": "Near miss — no patient reached"},
            {"code": "none", "display": "No harm"},
            {"code": "temporary", "display": "Temporary harm"},
            {"code": "permanent", "display": "Permanent harm"},
            {"code": "death", "display": "Death"},
        ],
        "safetyEventCategories": ["medication", "fall", "procedure",
                                  "equipment", "communication", "ai-agent",
                                  "other"],
        "rcaHarmLevels": ["permanent", "death"],
        "careGaps": [
            {"code": "diabetes-a1c-control", "registryKey": "diabetes",
             "display": "Diabetes: latest HbA1c at or below 8.0%",
             "conditionCode": "E11.9",
             "check": {"type": "latest-observation", "code": "4548-4",
                       "comparator": "lte", "threshold": 8.0,
                       "withinDays": 365}},
            {"code": "hypertension-bp-control", "registryKey": "hypertension",
             "display": "Hypertension: latest systolic BP below 140",
             "conditionCode": "I10",
             "check": {"type": "latest-observation", "code": "8480-6",
                       "comparator": "lt", "threshold": 140,
                       "withinDays": 365}},
        ],
    }


def _ops(session: Session) -> dict:
    cfg = published_config(session, "terminology", OPS_KEY)
    if cfg is not None and isinstance(cfg.payload, dict) and cfg.payload:
        return cfg.payload
    return default_ops_payload()


def _pathway(ops: dict, code: str) -> dict | None:
    for pathway in ops.get("activationPathways") or []:
        if isinstance(pathway, dict) and pathway.get("code") == code:
            return pathway
    return None


def _session_template(ops: dict, code: str) -> dict | None:
    for entry in ops.get("clinicSessions") or []:
        if isinstance(entry, dict) and entry.get("code") == code:
            return entry
    return None


# ---------------------------------------------------------------------------
# Governed forms seeded from the config's element lists
# ---------------------------------------------------------------------------

def _form(title: str, items: list[dict]) -> dict:
    return {"resourceType": "Questionnaire", "title": title, "item": items}


def _item(link_id, text, type_, required=False):
    return {"linkId": link_id, "text": text, "type": type_, "required": required}


_MSE_LABELS = {
    "presenting-complaint": "Presenting complaint",
    "vital-signs": "Vital signs recorded",
    "screening-exam-performed": "Medical screening examination performed",
    "exam-findings": "Screening examination findings",
    "stabilising-treatment": "Stabilising treatment provided / not indicated",
    "examiner": "Examining clinician (qualified medical person)",
}
_TRANSFER_LABELS = {
    "risks-benefits-discussed": "Transfer risks and benefits explained",
    "patient-consent": "Patient / representative consent to transfer",
    "receiving-facility": "Receiving facility name",
    "receiving-physician": "Receiving physician who has agreed",
    "transport-mode": "Transport mode and personnel",
    "medical-records-sent": "Medical records sent with the patient",
}


def default_form_payloads(ops: dict) -> dict[str, dict]:
    emtala = ops.get("emtala") or {}
    mse_items = [_item(code, _MSE_LABELS.get(code, code), "text", True)
                 for code in emtala.get("mseRequiredElements") or []]
    transfer_items = [_item(code, _TRANSFER_LABELS.get(code, code), "text", True)
                      for code in emtala.get("transferRequiredElements") or []]
    return {
        emtala.get("mseFormKey") or "emtala-mse": _form(
            "EMTALA medical screening examination", mse_items),
        emtala.get("transferFormKey") or "emtala-transfer-certification": _form(
            "EMTALA transfer certification", transfer_items),
        "handoff-ipass": _form("Structured handoff (I-PASS)", [
            _item("illness-severity", "I — Illness severity", "text", True),
            _item("patient-summary", "P — Patient summary", "text", True),
            _item("action-list", "A — Action list", "text", True),
            _item("situation-awareness", "S — Situation awareness and contingency planning", "text", True),
            _item("synthesis", "S — Synthesis by receiver (read-back)", "text", True),
        ]),
        "discharge-teachback": _form("Discharge instructions — teach-back", [
            _item("instructions-given", "Discharge instructions given to patient / caregiver", "text", True),
            _item("teach-back-completed", "Teach-back completed: patient restated medications, warning signs and follow-up in their own words", "boolean", True),
            _item("understanding-confirmed", "Understanding confirmed by the discharging clinician", "boolean", True),
        ]),
        # Depth pass (Workstream 2): interdisciplinary rounds checklist
        # — one governed form per patient per day, surfaced on the
        # rounds list as rounds_checklist_today.
        "rounds-checklist": _form("Interdisciplinary rounds checklist", [
            _item("overnight-events", "Overnight events", "text", True),
            _item("lines-devices-reviewed", "Lines and devices reviewed", "boolean", True),
            _item("vte-prophylaxis", "VTE prophylaxis reviewed", "boolean", True),
            _item("glucose-control", "Glucose control", "text"),
            _item("pain-control", "Pain control", "text"),
            _item("discharge-barriers", "Discharge barriers", "text"),
            _item("family-updated", "Family updated", "boolean"),
        ]),
        # Depth pass (Workstream 2): discharge medication
        # reconciliation — the readiness board's fourth element,
        # config-gated by dischargeGuards.requireMedRec.
        "discharge-med-rec": _form("Discharge medication reconciliation", [
            _item("home-medications-reviewed", "Home medications reviewed", "boolean", True),
            _item("medication-changes", "Medication changes explained to the patient", "text", True),
            _item("new-prescriptions", "New prescriptions", "text"),
            _item("discontinued-medications", "Discontinued medications", "text"),
            _item("allergies-rechecked", "Allergies rechecked", "boolean", True),
        ]),
    }


# ---------------------------------------------------------------------------
# Seeds: config + forms + dashboards + boarding rule + compliance
# register entries + EMTALA pack draft + safety-triage agent draft
# ---------------------------------------------------------------------------

def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    if list_config_versions(session, kind, key):
        return
    from .models import ConfigStatus, StudioConfig
    row = StudioConfig(kind=kind, key=key, version=1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1,
                          "seeded_default": True})
    append_audit(session, actor="system", action="studio.config.publish",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1,
                          "superseded_version": None, "seeded_default": True})


def _seed_pack_draft(session: Session, key: str, payload: dict) -> None:
    if list_config_versions(session, "regulatory_pack", key):
        return
    from .models import ConfigStatus, StudioConfig
    row = StudioConfig(kind="regulatory_pack", key=key, version=1,
                       status=ConfigStatus.draft, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:regulatory_pack/{key}@v1",
                 payload={"kind": "regulatory_pack", "key": key, "version": 1,
                          "seeded_default": True})


def default_requirement_seeds() -> list[dict]:
    return [
        {"body": "EMTALA", "citation": "42 CFR 489.24(a), (d)",
         "title": "Medical screening examination and stabilisation",
         "control": ("Every person who comes to the emergency department "
                     "receives a medical screening examination by a "
                     "qualified medical person; the required documentation "
                     "elements are the published continuum-ops config list "
                     "rendered as the governed EMTALA MSE form, and an "
                     "emergency medical condition is stabilised (or "
                     "certified for transfer) before the record closes."),
         "implementation_ref": "backend/app/continuum.py (MSE form + "
                               "transfer-out gate); terminology/continuum-ops emtala section",
         "evidence_source": "audit:continuum.er.mse"},
        {"body": "EMTALA", "citation": "42 CFR 489.24(e)",
         "title": "Appropriate transfer — certification before transfer",
         "control": ("A transfer out cannot be recorded until a transfer "
                     "certification (risks/benefits discussed, patient "
                     "consent, receiving facility + physician, transport "
                     "mode, records sent) is on file for the encounter, on "
                     "top of the screening examination; the endpoint "
                     "refuses (422, naming the missing elements) rather "
                     "than letting the record close incomplete."),
         "implementation_ref": "backend/app/continuum.py POST /continuum/er/encounters/{id}/transfer-out",
         "evidence_source": "audit:continuum.er.transfer"},
        {"body": "EMTALA", "citation": "42 CFR 489.20(r)(3)",
         "title": "Central log of persons who come to the ED",
         "control": ("Every ED arrival, disposition and transfer is in "
                     "the operational record (ED board states + the "
                     "hash-chained audit chain, which carries arrival, "
                     "LWBS, admission, departure and transfer entries); "
                     "the log is retained per the EMTALA pack parameter "
                     "centralLogRetentionYears."),
         "implementation_ref": "backend/app/boards.py (ED board); "
                               "backend/app/audit.py (hash chain)",
         "evidence_source": "audit:ed.encounter."},
        {"body": "CMS", "citation": "42 CFR 482.43",
         "title": "Discharge planning process (Conditions of Participation)",
         "control": ("Discharge planning begins at admission: an "
                     "expected discharge date and a barrier list ride on "
                     "the admission's discharge plan (Phase 6), the "
                     "discharge-readiness board shows checklist, "
                     "teach-back and follow-up-booking state per patient, "
                     "and a site may publish the continuum-ops guard that "
                     "refuses discharge completion until a follow-up "
                     "appointment is booked."),
         "implementation_ref": "backend/app/inpatient.py (discharge plan); "
                               "backend/app/continuum.py (readiness board + "
                               "follow-up guard in discharge)",
         "evidence_source": "audit:inpatient.encounter.discharge"},
        {"body": "The Joint Commission", "citation": "Provision of Care (PC) — handoff communications",
         "title": "Structured handoff communication",
         "control": ("Handoffs use the governed I-PASS form (illness "
                     "severity, patient summary, action list, situation "
                     "awareness, synthesis by receiver); the form's "
                     "required items are enforced by the ordinary form "
                     "machinery, and every handoff is a stored, auditable "
                     "QuestionnaireResponse on the encounter."),
         "implementation_ref": "backend/app/continuum.py (handoff-ipass form "
                               "+ handoffs endpoints)",
         "evidence_source": "audit:continuum.handoff.create"},
        {"body": "CMS/The Joint Commission", "citation": "42 CFR 482.43 / NPSG — discharge instructions",
         "title": "Discharge instructions with teach-back",
         "control": ("Discharge readiness includes a teach-back "
                     "sign-off: the governed discharge-teachback form "
                     "records that instructions were given and the "
                     "patient restated medications, warning signs and "
                     "follow-up in their own words, confirmed by the "
                     "discharging clinician, before the readiness board "
                     "shows the patient as ready."),
         "implementation_ref": "backend/app/continuum.py (discharge-teachback "
                               "form + readiness computation)",
         "evidence_source": "audit:continuum.aftercare.teach-back"},
    ]


def default_emtala_pack_payload() -> dict:
    return {
        "body": "EMTALA",
        "jurisdiction": "US",
        "parameters": {
            "mseRequiredElementsFrom": "terminology/continuum-ops emtala.mseRequiredElements",
            "transferCertificationRequired": True,
            "centralLogRetentionYears": 5,
            "onCallRosterRequired": True,
            "signageRequired": True,
        },
        "requirements": ["42 CFR 489.24(a), (d)", "42 CFR 489.24(e)",
                         "42 CFR 489.20(r)(3)"],
        "notes": ("Emergency Medical Treatment and Labor Act baseline. "
                  "The operational controls live in app.continuum (MSE + "
                  "transfer-certification forms as governed Studio forms, "
                  "the transfer-out gate) and app.boards (the ED central "
                  "log). The required-element lists themselves are the "
                  "continuum-ops config, so an element change is a config "
                  "publish. Seeded as a draft (Phase 12)."),
    }


def safety_triage_agent_payload() -> dict:
    return {
        "description": ("Phase 12 (Track D, Armstrong pattern): triage "
                        "assistant for patient-safety event reports — "
                        "suggests a category and harm classification from "
                        "the report text and drafts the first reviewer "
                        "follow-up questions. Draft only: a human safety "
                        "reviewer owns triage; the agent never closes a "
                        "report, never contacts the reporter, and never "
                        "sees a confidential reporter's identity."),
        "model": "",
        "instructions": (
            "You are the OpenCare safety triage assistant. Given ONE "
            "safety event report in the prompt, propose (1) a category "
            "from the configured taxonomy, (2) a harm level from the "
            "configured taxonomy with the sentence in the report that "
            "supports it, and (3) up to three follow-up questions a "
            "reviewer should ask. Use ONLY facts in the report — never "
            "invent clinical details, people or outcomes. Mark every "
            "output as a draft suggestion for the human safety "
            "reviewer, who makes the actual classification."),
        "tools": ["opencare_api_call"],
        "apiAllowlist": ["/continuum"],
        "modalities": {"text": True, "image": False, "audio": False},
        "temperature": 0.2,
        "maxSteps": 4,
    }


def ensure_continuum_defaults(session: Session) -> None:
    """Seed the continuum-ops config, the governed forms, the
    discharge-readiness + unit-safety dashboards (Track B mechanism),
    the boarding-escalation rule, the EMTALA draft pack + the Phase 12
    compliance register entries, and the safety-triage agent DRAFT.
    All idempotent; all seeded through governance, none self-enabled
    beyond the published-by-default operational config."""
    ops = default_ops_payload()
    _seed_published(session, "terminology", OPS_KEY, ops)
    for key, payload in default_form_payloads(ops).items():
        _seed_published(session, "form", key, payload)
    _seed_published(session, "dashboard", "discharge-readiness", {
        "title": "Discharge readiness",
        "description": ("Phase 12: who can leave today, and what is "
                        "still missing — computed live from plans, "
                        "checklists, teach-back and bookings."),
        "widgets": [
            {"id": "ready", "title": "Patients ready for discharge",
             "endpoint": "/continuum/inpatient/discharge-readiness",
             "display": "value", "field": "ready"},
            {"id": "awaiting", "title": "Admissions tracked on the board",
             "endpoint": "/continuum/inpatient/discharge-readiness",
             "display": "total"},
            {"id": "boarding", "title": "Patients boarding (awaiting a bed)",
             "endpoint": "/continuum/er/boarding", "display": "total"},
        ]})
    _seed_published(session, "dashboard", "unit-safety", {
        "title": "Unit safety",
        "description": ("Phase 12: safety event volume and open RCA "
                        "work, from the Armstrong-pattern reporting "
                        "system."),
        "widgets": [
            {"id": "events", "title": "Safety events (all time)",
             "endpoint": "/continuum/safety/summary", "display": "total"},
            {"id": "open-rca", "title": "Open RCA work items",
             "endpoint": "/continuum/safety/summary",
             "display": "value", "field": "rca_open"},
        ]})
    from .rules import _seed_published as seed_rule
    seed_rule(session, "rule", "ed-boarding-escalation", {
        "name": "ED boarding escalation",
        "description": ("Phase 12: when the boarding count (admissions "
                        "awaiting a bed) reaches the configured "
                        "threshold on any bed/ADT movement, open an "
                        "escalation Task for the bed-management team. "
                        "The threshold lives in this rule's condition."),
        "event": "continuum.boarding.changed",
        "condition": {"signal": "boarding_count", "op": "gte", "value": 4},
        "action": {"type": "create_task",
                   "params": {"title": "ED boarding escalation — review "
                                       "bed assignments",
                              "ownerRole": "bed-management"}},
        "enabled": True,
    })
    _seed_pack_draft(session, "emtala", default_emtala_pack_payload())
    existing = {(r.body, r.citation) for r in
                session.scalars(select(ComplianceRequirement)).all()}
    added = 0
    for seed in default_requirement_seeds():
        if (seed["body"], seed["citation"]) in existing:
            continue
        session.add(ComplianceRequirement(created_by="system", **seed))
        added += 1
    if added:
        session.flush()
        append_audit(session, actor="system",
                     action="compliance.register.seed", entity="register",
                     payload={"entries_added": added, "phase": 12,
                              "bodies": sorted({s["body"] for s in
                                                default_requirement_seeds()})})
    from .agents import _seed_draft as seed_agent
    seed_agent(session, "safety-triage-assistant",
               safety_triage_agent_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Shared derived state: admissions awaiting a bed ("boarding")
# ---------------------------------------------------------------------------

def _bedless_admissions(session: Session) -> list[FhirResource]:
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Encounter",
            FhirResource.status == "in-progress")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    out = []
    for row in rows:
        body = row.body or {}
        if inpatient._class_code(body) != "IMP":
            continue
        if inpatient._current_bed(body) is None:
            out.append(row)
    return out


def _boarding_source(session: Session, body: dict) -> str:
    part_of = ((body or {}).get("partOf") or {}).get("reference") or ""
    if part_of.startswith("Encounter/"):
        try:
            parent = fhir._get_row("Encounter", part_of.split("/", 1)[1], session)
        except HTTPException:
            return "direct"
        if inpatient._class_code(parent.body or {}) == "EMER":
            return "ed"
    return "direct"


def boarding_snapshot(session: Session, now: datetime | None = None) -> dict:
    """Admissions awaiting a bed, computed live: source (ED vs
    direct), wait since the admission was created, requested unit
    (the ward named at ED admit, when present)."""
    now = now or _now()
    items = []
    for row in _bedless_admissions(session):
        body = row.body or {}
        start = _parse_dt(((body.get("period") or {}).get("start"))) \
            or _aware(row.created_at) or now
        requested_unit = None
        for entry in body.get("location") or []:
            if isinstance(entry, dict):
                requested_unit = ((entry.get("location") or {}).get("display")
                                  or requested_unit)
        items.append({
            "encounter_id": row.resource_id,
            "patient_ref": inpatient._subject_ref(body),
            "source": _boarding_source(session, body),
            "requested_unit": requested_unit,
            "waiting_since": start.isoformat(),
            "wait_minutes": max(0, int((now - start).total_seconds() // 60)),
        })
    items.sort(key=lambda i: i["waiting_since"])
    ed_items = [i for i in items if i["source"] == "ed"]
    return {"items": items, "total": len(items),
            "ed_total": len(ed_items),
            "oldest_wait_minutes": max((i["wait_minutes"] for i in items),
                                       default=0),
            "generated_at": now.isoformat()}


def emit_boarding_event(session: Session, actor: str) -> None:
    """Publish the live boarding state to the rules engine (the
    seeded ed-boarding-escalation rule turns a threshold breach into
    a bed-management Task). Called in-transaction by the writers that
    move beds or create admissions (app.inpatient ADT events,
    app.boards ED admit, this module's assign path)."""
    snap = boarding_snapshot(session)
    evaluate_rules(session, "continuum.boarding.changed",
                   {"boarding_count": snap["total"],
                    "ed_boarding_count": snap["ed_total"],
                    "oldest_wait_minutes": snap["oldest_wait_minutes"]},
                   actor=actor)


def followup_missing(session: Session, patient_ref: str) -> bool:
    """The Phase 12 discharge guard (consumed by app.inpatient's
    discharge endpoint): True only when the PUBLISHED continuum-ops
    config requires a booked follow-up before discharge AND the
    patient has no future appointment. Ships OFF (guard inert) so
    existing behaviour is preserved until a site publishes it on —
    the Phase 7 prior-auth pattern."""
    ops = _ops(session)
    guards = ops.get("dischargeGuards") or {}
    if guards.get("followUpRequired") is not True:
        return False
    return _future_appointment(session, patient_ref) is None


def _future_appointment(session: Session, patient_ref: str) -> FhirResource | None:
    now_naive = _now().replace(tzinfo=None)
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Appointment",
            FhirResource.status.in_(["booked", "proposed", "pending",
                                     "arrived", "checked-in"]))
        .order_by(FhirResource.start_at.asc())).all()
    for row in rows:
        if row.start_at is not None and row.start_at < now_naive:
            continue
        if _appointment_patient_ref(row.body or {}) == patient_ref:
            return row
    return None


def _appointment_patient_ref(body: dict) -> str | None:
    for participant in (body or {}).get("participant") or []:
        if isinstance(participant, dict):
            ref = ((participant.get("actor") or {}).get("reference") or "")
            if ref.startswith("Patient/"):
                return ref
    return None


# ---------------------------------------------------------------------------
# ER operations
# ---------------------------------------------------------------------------

def _esi_target(ops: dict, acuity) -> int | None:
    targets = ops.get("esiTargetsMinutes") or {}
    value = targets.get(str(acuity)) if acuity is not None else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@router.get("/er/board")
def er_board(session: Session = Depends(get_db),
             user: User = Depends(require_capability(READ))):
    """The ER operations board: the Phase 4B tracking board enriched
    with what running a department needs — per-patient door-to-provider
    target (from the ESI config) with breach flags, the split-flow
    queues (fast track = configured ESI levels still waiting), and the
    facility diversion/surge state as the board's banner."""
    ops = _ops(session)
    now = _now()
    active_states, _ = boards._ed_state_sets(session)
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Encounter")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    fast_levels = set(ops.get("fastTrackEsiLevels") or [])
    groups: dict[str, list[dict]] = {state: [] for state in active_states}
    fast_track, main_queue = [], []
    breaches = 0
    for row in rows:
        body = row.body or {}
        if boards._class_code(body) != "EMER":
            continue
        state = body.get("status")
        if state not in groups:
            continue
        entry = boards._board_entry(row, now)
        target = _esi_target(ops, entry["acuity"])
        entry["target_minutes"] = target
        # The door-to-provider clock runs until the patient is in
        # treatment; after that the target is history, not a breach.
        entry["target_breached"] = bool(
            target is not None and state in ("waiting", "triage")
            and entry["wait_minutes"] > target)
        if entry["target_breached"]:
            breaches += 1
        groups[state].append(entry)
        if state in ("waiting", "triage"):
            (fast_track if entry["acuity"] in fast_levels
             else main_queue).append(entry)
    ops_state = _facility_state(session)
    return {"generated_at": now.isoformat(),
            "counts": {**{s: len(v) for s, v in groups.items()},
                       "total": sum(len(v) for v in groups.values()),
                       "target_breaches": breaches},
            "groups": groups,
            "queues": {"fast_track": fast_track, "main": main_queue},
            "diversion": ops_state["diversion"],
            "surge_level": ops_state["surge_level"]}


@router.get("/er/boarding")
def er_boarding(session: Session = Depends(get_db),
                user: User = Depends(require_capability(READ))):
    """Boarding board: every admission awaiting a bed (ED boarders
    and direct admissions alike), oldest first, with the wait that
    the escalation rule watches."""
    snap = boarding_snapshot(session)
    return {**snap, "total": snap["total"], "items": snap["items"]}


def _facility_state(session: Session) -> dict:
    row = session.scalars(select(FacilityOpsState).where(
        FacilityOpsState.key == "diversion")).first()
    if row is None:
        return {"diversion": "open", "surge_level": "normal",
                "reason": None, "updated_by": None, "updated_at": None}
    value = row.value or {}
    return {"diversion": value.get("status") or "open",
            "surge_level": value.get("surgeLevel") or "normal",
            "reason": value.get("reason"),
            "updated_by": row.updated_by,
            "updated_at": _aware(row.updated_at).isoformat()
            if row.updated_at else None}


@router.get("/er/diversion")
def get_diversion(session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    ops = _ops(session)
    return {**_facility_state(session),
            "states": ops.get("diversionStates") or [],
            "surge_levels": ops.get("surgeLevels") or []}


@router.post("/er/diversion")
async def set_diversion(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability(WORK))):
    """Set the facility diversion status / surge level. States are
    config data; every change is audited (the after-action record)."""
    payload = await _json_body(request)
    ops = _ops(session)
    status = _require_str(payload, "status")
    valid = {s.get("code") for s in ops.get("diversionStates") or []
             if isinstance(s, dict)}
    if status not in valid:
        raise HTTPException(status_code=422,
                            detail=f"Unknown diversion status '{status}': "
                                   f"not in the published continuum-ops config")
    surge = payload.get("surgeLevel") or payload.get("surge_level")
    valid_surge = {s.get("code") for s in ops.get("surgeLevels") or []
                   if isinstance(s, dict)}
    if surge is not None and surge not in valid_surge:
        raise HTTPException(status_code=422,
                            detail=f"Unknown surge level '{surge}'")
    row = session.scalars(select(FacilityOpsState).where(
        FacilityOpsState.key == "diversion")).first()
    current = dict(row.value or {}) if row else {}
    current["status"] = status
    if surge is not None:
        current["surgeLevel"] = surge
    current["reason"] = payload.get("reason") if isinstance(
        payload.get("reason"), str) else current.get("reason")
    if row is None:
        row = FacilityOpsState(key="diversion", value=current,
                               updated_by=user.username)
        session.add(row)
    else:
        row.value = current
        row.updated_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="continuum.ops.diversion",
                 entity="facility/diversion",
                 payload={"status": status, "surge_level": surge,
                          "reason": current.get("reason")})
    session.commit()
    return _facility_state(session)


# --- Activations (STEMI / stroke / trauma) --------------------------------

def _activation_out(row: EdActivation, now: datetime) -> dict:
    started = _aware(row.started_at) or now
    milestones = []
    for m in row.milestones or []:
        entry = dict(m)
        recorded = _parse_dt(entry.get("recorded_at"))
        target = entry.get("targetMinutes")
        if recorded is not None:
            entry["elapsed_minutes"] = max(
                0, int((recorded - started).total_seconds() // 60))
            entry["breached"] = bool(target is not None
                                     and entry["elapsed_minutes"] > target)
        else:
            entry["elapsed_minutes"] = None
            entry["breached"] = bool(
                row.status == "open" and target is not None
                and (now - started).total_seconds() // 60 > target)
        milestones.append(entry)
    end = _aware(row.closed_at) or now
    elapsed = max(0, int((end - started).total_seconds() // 60)) \
        if started else 0
    return {"id": row.activation_id, "encounter_id": row.encounter_id,
            "patient_ref": row.patient_ref, "pathway": row.pathway,
            "pathway_display": row.pathway_display,
            "target_minutes": row.target_minutes, "status": row.status,
            "outcome": row.outcome, "started_by": row.started_by,
            "started_at": started.isoformat() if started else None,
            "elapsed_minutes": elapsed,
            "target_breached": bool(row.target_minutes
                                    and elapsed > row.target_minutes),
            "milestones": milestones}


@router.post("/er/activations", status_code=201)
async def start_activation(request: Request, session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    """Start a time-critical pathway activation on an ED encounter.
    The pathway's targets and milestone list are copied from the
    published config at start time — the record keeps the targets it
    was started under."""
    payload = await _json_body(request)
    encounter_id = _require_str(payload, "encounter_id")
    pathway_code = _require_str(payload, "pathway")
    ops = _ops(session)
    pathway = _pathway(ops, pathway_code)
    if pathway is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown activation pathway "
                                   f"'{pathway_code}': not in the published "
                                   "continuum-ops config")
    row_enc = boards._ed_row(session, encounter_id)
    existing = session.scalars(select(EdActivation).where(
        EdActivation.encounter_id == encounter_id,
        EdActivation.pathway == pathway_code,
        EdActivation.status == "open")).first()
    if existing is not None:
        raise HTTPException(status_code=409,
                            detail=f"A {pathway_code} activation is already "
                                   f"open for Encounter/{encounter_id}")
    milestones = [{"code": m.get("code"), "display": m.get("display"),
                   "targetMinutes": m.get("targetMinutes"),
                   "recorded_at": None, "recorded_by": None}
                  for m in pathway.get("milestones") or []
                  if isinstance(m, dict) and m.get("code")]
    activation = EdActivation(
        activation_id=str(uuid.uuid4()), encounter_id=encounter_id,
        patient_ref=boards._subject_ref(row_enc.body or {}),
        pathway=pathway_code,
        pathway_display=pathway.get("display") or pathway_code,
        target_minutes=pathway.get("targetMinutes") or 0,
        milestones=milestones, started_by=user.username)
    session.add(activation)
    session.flush()
    append_audit(session, actor=user.username, action="continuum.er.activation.start",
                 entity=f"Encounter/{encounter_id}",
                 payload={"activation_id": activation.activation_id,
                          "pathway": pathway_code,
                          "patient": activation.patient_ref})
    session.commit()
    return _activation_out(activation, _now())


@router.get("/er/activations")
def list_activations(status: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    stmt = select(EdActivation).order_by(EdActivation.started_at.desc(),
                                         EdActivation.id.desc())
    if status:
        stmt = stmt.where(EdActivation.status == status)
    rows = session.scalars(stmt).all()
    now = _now()
    items = [_activation_out(r, now) for r in rows]
    return {"items": items, "total": len(items)}


def _activation_row(session: Session, activation_id: str) -> EdActivation:
    row = session.scalars(select(EdActivation).where(
        EdActivation.activation_id == activation_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Activation {activation_id} not found")
    return row


@router.post("/er/activations/{activation_id}/milestone")
async def record_milestone(activation_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    code = _require_str(payload, "code")
    row = _activation_row(session, activation_id)
    if row.status != "open":
        raise HTTPException(status_code=409,
                            detail=f"Activation {activation_id} is "
                                   f"'{row.status}'; milestones need an open activation")
    milestones = [dict(m) for m in row.milestones or []]
    target = next((m for m in milestones if m.get("code") == code), None)
    if target is None:
        raise HTTPException(status_code=422,
                            detail=f"Milestone '{code}' is not part of the "
                                   f"{row.pathway} pathway as started")
    if target.get("recorded_at"):
        raise HTTPException(status_code=409,
                            detail=f"Milestone '{code}' was already recorded "
                                   f"at {target['recorded_at']}")
    target["recorded_at"] = _now_iso()
    target["recorded_by"] = user.username
    row.milestones = milestones
    session.flush()
    append_audit(session, actor=user.username,
                 action="continuum.er.activation.milestone",
                 entity=f"Encounter/{row.encounter_id}",
                 payload={"activation_id": activation_id, "pathway": row.pathway,
                          "milestone": code, "patient": row.patient_ref})
    session.commit()
    return _activation_out(row, _now())


@router.post("/er/activations/{activation_id}/close")
async def close_activation(activation_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    row = _activation_row(session, activation_id)
    if row.status != "open":
        raise HTTPException(status_code=409,
                            detail=f"Activation {activation_id} is already '{row.status}'")
    row.status = "closed"
    row.outcome = payload.get("outcome") if isinstance(
        payload.get("outcome"), str) else None
    row.closed_at = _now()
    session.flush()
    append_audit(session, actor=user.username,
                 action="continuum.er.activation.close",
                 entity=f"Encounter/{row.encounter_id}",
                 payload={"activation_id": activation_id, "pathway": row.pathway,
                          "outcome": row.outcome, "patient": row.patient_ref})
    session.commit()
    return _activation_out(row, _now())


# --- LWBS capture -----------------------------------------------------------

@router.post("/er/encounters/{encounter_id}/lwbs")
async def record_lwbs(encounter_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability(WORK))):
    """Left-without-being-seen capture: the patient was still waiting
    (waiting / triage — by definition not yet in treatment) and left.
    The encounter closes as departed with the LWBS marker on it, so
    the rate is computable from the record, and the act is audited."""
    payload = await _json_body(request)
    row = boards._ed_row(session, encounter_id)
    body = dict(row.body or {})
    current = body.get("status")
    if current not in ("waiting", "triage"):
        raise HTTPException(status_code=409,
                            detail=f"LWBS requires a patient still waiting "
                                   f"to be seen; Encounter/{encounter_id} is "
                                   f"'{current}'")
    body["status"] = "departed"
    period = dict(body.get("period") or {})
    period["end"] = _now_iso()
    body["period"] = period
    boards._set_extension(body, LWBS_EXTENSION, valueBoolean=True)
    row.body = body
    row.status = "departed"
    row.version += 1
    session.flush()
    note = payload.get("note") if isinstance(payload.get("note"), str) else None
    append_audit(session, actor=user.username, action="continuum.er.lwbs",
                 entity=f"Encounter/{encounter_id}",
                 payload={"patient": boards._subject_ref(body),
                          "from_status": current, "note": note,
                          "version": row.version})
    session.commit()
    return {"ed_encounter": fhir._render(row), "lwbs": True}


# --- EMTALA: MSE + transfer certification gate ------------------------------

def _form_response_for(session: Session, form_key: str,
                       encounter_id: str) -> FhirResource | None:
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "QuestionnaireResponse")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    for row in rows:
        body = row.body or {}
        if body.get("questionnaire") != form_key:
            continue
        if ((body.get("encounter") or {}).get("reference")
                == f"Encounter/{encounter_id}"):
            return row
    return None


def _form_response_today(session: Session, form_key: str,
                         encounter_id: str, now: datetime) -> bool:
    """True when a response to `form_key` exists for the encounter
    whose authored date is TODAY (the rounds board's per-day fact).
    Falls back to the row's creation time when authored is absent."""
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "QuestionnaireResponse")).all()
    for row in rows:
        body = row.body or {}
        if body.get("questionnaire") != form_key:
            continue
        if ((body.get("encounter") or {}).get("reference")
                != f"Encounter/{encounter_id}"):
            continue
        authored = _parse_dt(body.get("authored")) \
            or _aware(row.created_at)
        if authored is not None and authored.date() == now.date():
            return True
    return False


def _answer_value(answers, link_id: str):
    for item in answers or []:
        if isinstance(item, dict) and item.get("linkId") == link_id:
            for answer in item.get("answer") or []:
                if isinstance(answer, dict):
                    for key in ("valueString", "valueBoolean", "valueInteger",
                                "valueDateTime", "valueDate"):
                        if key in answer:
                            return answer[key]
    return None


@router.post("/er/encounters/{encounter_id}/mse", status_code=201)
async def record_mse(encounter_id: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(WORK))):
    """Record the medical screening examination for an ED encounter:
    a QuestionnaireResponse against the governed EMTALA MSE form
    (required elements enforced by the ordinary form machinery)."""
    payload = await _json_body(request)
    row = boards._ed_row(session, encounter_id)
    ops = _ops(session)
    form_key = (ops.get("emtala") or {}).get("mseFormKey") or "emtala-mse"
    patient_ref = boards._subject_ref(row.body or {})
    stored = documentation._store_response(
        session, user, form_key, patient_ref, payload.get("answers"),
        encounter_ref=f"Encounter/{encounter_id}")
    append_audit(session, actor=user.username, action="continuum.er.mse",
                 entity=f"Encounter/{encounter_id}",
                 payload={"response_id": stored["id"], "form": form_key,
                          "patient": patient_ref})
    session.commit()
    return {"response": stored, "form": form_key}


@router.post("/er/encounters/{encounter_id}/transfer-out")
async def transfer_out(encounter_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability(WORK))):
    """EMTALA transfer out. The ER record CANNOT close without (1) a
    medical screening examination on file for this encounter and
    (2) a complete transfer certification — both refusals are 422s
    naming what is missing. On success the encounter departs through
    the published ED workflow's governed exit, with the transfer
    marker and the certification linked."""
    payload = await _json_body(request)
    row = boards._ed_row(session, encounter_id)
    ops = _ops(session)
    emtala = ops.get("emtala") or {}
    mse_key = emtala.get("mseFormKey") or "emtala-mse"
    transfer_key = emtala.get("transferFormKey") or "emtala-transfer-certification"
    patient_ref = boards._subject_ref(row.body or {})
    mse = _form_response_for(session, mse_key, encounter_id)
    if mse is None:
        raise HTTPException(
            status_code=422,
            detail=("EMTALA: no medical screening examination is on file "
                    f"for Encounter/{encounter_id}; a transfer cannot be "
                    "recorded before the screening examination is "
                    "documented. Missing elements: "
                    + ", ".join(emtala.get("mseRequiredElements") or [])))
    boards._ed_finish(session, user, row, "departed", "transfer")
    answers = payload.get("answers")
    cert = documentation._store_response(
        session, user, transfer_key, patient_ref, answers,
        encounter_ref=f"Encounter/{encounter_id}")
    body = dict(row.body or {})
    body["status"] = "departed"
    period = dict(body.get("period") or {})
    period["end"] = _now_iso()
    body["period"] = period
    boards._set_extension(body, TRANSFER_OUT_EXTENSION,
                          valueReference={"reference":
                                          f"QuestionnaireResponse/{cert['id']}"})
    row.body = body
    row.status = "departed"
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="continuum.er.transfer",
                 entity=f"Encounter/{encounter_id}",
                 payload={"patient": patient_ref,
                          "mse_response_id": mse.resource_id,
                          "transfer_response_id": cert["id"],
                          "receiving_facility": _answer_value(
                              answers, "receiving-facility"),
                          "version": row.version})
    session.commit()
    return {"ed_encounter": fhir._render(row),
            "mse_response_id": mse.resource_id,
            "transfer_response_id": cert["id"]}


# --- Results-callback queue -------------------------------------------------

def _discharged_patient_refs(session: Session) -> set[str]:
    refs = set()
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Encounter",
        FhirResource.status.in_(["departed", "finished"]))).all()
    for row in rows:
        body = row.body or {}
        if boards._class_code(body) in ("EMER", "IMP"):
            ref = boards._subject_ref(body)
            if ref:
                refs.add(ref)
    return refs


def _pending_callbacks(session: Session) -> list[dict]:
    """Results still pending for patients who have already left: a
    DiagnosticReport whose lab order is not yet released, for a
    patient with a departed/finished encounter. Derived live."""
    from . import lab as lab_module  # lazy: lab is a large module
    discharged = _discharged_patient_refs(session)
    if not discharged:
        return []
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "DiagnosticReport")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = []
    callbacks = {c.report_id: c for c in
                 session.scalars(select(ResultCallback)).all()}
    for report in rows:
        if report.patient_ref not in discharged:
            continue
        order = lab_module._report_order(session, report)
        if order is not None and (order.body or {}).get("status") == "released":
            continue
        callback = callbacks.get(report.resource_id)
        items.append({"report_id": report.resource_id,
                      "patient_ref": report.patient_ref,
                      "report_status": (report.body or {}).get("status"),
                      "created_at": _aware(report.created_at).isoformat()
                      if report.created_at else None,
                      "callback_status": callback.status if callback else "pending",
                      "callback_outcome": callback.outcome if callback else None})
    return items


@router.get("/er/callbacks")
def callback_queue(session: Session = Depends(get_db),
                   user: User = Depends(require_capability(READ))):
    ops = _ops(session)
    sla = (ops.get("inboxSlaMinutes") or {}).get("callback")
    now = _now()
    pending, completed = [], []
    for item in _pending_callbacks(session):
        created = _parse_dt(item["created_at"]) or now
        item["age_minutes"] = max(0, int((now - created).total_seconds() // 60))
        item["sla_minutes"] = sla
        item["sla_breached"] = bool(sla and item["age_minutes"] > sla)
        (completed if item["callback_status"] == "completed"
         else pending).append(item)
    pending.sort(key=lambda i: i["created_at"] or "")
    return {"items": pending, "completed": completed,
            "total": len(pending)}


@router.post("/er/callbacks/{report_id}/complete")
async def complete_callback(report_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    report = fhir._get_row("DiagnosticReport", report_id, session)
    row = session.scalars(select(ResultCallback).where(
        ResultCallback.report_id == report_id)).first()
    if row is None:
        row = ResultCallback(report_id=report_id,
                             patient_ref=report.patient_ref or "",
                             recorded_by=user.username)
        session.add(row)
    row.status = "completed"
    row.outcome = payload.get("outcome") if isinstance(
        payload.get("outcome"), str) else None
    row.note = payload.get("note") if isinstance(
        payload.get("note"), str) else None
    row.recorded_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="continuum.er.callback",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"patient": report.patient_ref,
                          "outcome": row.outcome})
    session.commit()
    return {"report_id": report_id, "status": "completed",
            "outcome": row.outcome, "recorded_by": row.recorded_by}


# ---------------------------------------------------------------------------
# Inpatient operations: assignments, rounds, readiness, handoffs, EVS
# ---------------------------------------------------------------------------

def _bed_unit_map(session: Session) -> dict[str, str]:
    _units, beds = inpatient._location_concepts(session)
    return {c["code"]: (c.get("parent") or "") for c in beds
            if isinstance(c, dict) and c.get("code")}


def _admission_unit(session: Session, body: dict,
                    bed_units: dict[str, str] | None = None) -> str | None:
    bed = inpatient._current_bed(body)
    if bed is None:
        return None
    if bed_units is None:
        bed_units = _bed_unit_map(session)
    return bed_units.get(bed)


def _active_admissions(session: Session) -> list[FhirResource]:
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Encounter",
            FhirResource.status == "in-progress")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    return [r for r in rows
            if inpatient._class_code(r.body or {}) == "IMP"]


@router.get("/inpatient/assignments")
def list_assignments(unit: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    """Acuity-based assignment board: who is nursing whom, each
    patient's acuity, and each nurse's total acuity load against the
    configured maximum (overload flagged, computed on read)."""
    ops = _ops(session)
    max_load = (ops.get("nurseLoad") or {}).get("maxAcuityPerNurse")
    active = {r.resource_id: r for r in _active_admissions(session)}
    bed_units = _bed_unit_map(session)
    items, loads = [], {}
    for row in session.scalars(select(NurseAssignment).order_by(
            NurseAssignment.nurse, NurseAssignment.id)).all():
        if row.encounter_id not in active:
            continue
        if unit and row.unit != unit:
            continue
        loads[row.nurse] = loads.get(row.nurse, 0) + row.acuity
        items.append({"encounter_id": row.encounter_id,
                      "patient_ref": row.patient_ref, "unit": row.unit,
                      "nurse": row.nurse, "acuity": row.acuity,
                      "assigned_by": row.assigned_by})
    nurses = [{"nurse": nurse, "acuity_load": load,
               "max_acuity": max_load,
               "overloaded": bool(max_load and load > max_load)}
              for nurse, load in sorted(loads.items())]
    unassigned = [{"encounter_id": r.resource_id,
                   "patient_ref": inpatient._subject_ref(r.body or {}),
                   "unit": _admission_unit(session, r.body or {}, bed_units)}
                  for r in active.values()
                  if r.resource_id not in {i["encounter_id"] for i in items}]
    return {"items": items, "nurses": nurses, "unassigned": unassigned,
            "total": len(items)}


@router.post("/inpatient/assignments", status_code=201)
async def set_assignment(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    encounter_id = _require_str(payload, "encounter_id")
    nurse = _require_str(payload, "nurse")
    acuity = payload.get("acuity")
    ops = _ops(session)
    levels = ops.get("acuityLevels") or [1, 2, 3, 4, 5]
    if isinstance(acuity, bool) or not isinstance(acuity, int) \
            or acuity not in levels:
        raise HTTPException(status_code=422,
                            detail=f"'acuity' must be one of the configured "
                                   f"acuity levels {levels}")
    row = inpatient._inpatient_row(session, encounter_id)
    body = row.body or {}
    if body.get("status") != "in-progress":
        raise HTTPException(status_code=409,
                            detail=f"Assignments need an in-progress "
                                   f"admission; Encounter/{encounter_id} is "
                                   f"'{body.get('status')}'")
    unit_code = _admission_unit(session, body)
    existing = session.scalars(select(NurseAssignment).where(
        NurseAssignment.encounter_id == encounter_id)).first()
    if existing is None:
        existing = NurseAssignment(
            encounter_id=encounter_id,
            patient_ref=inpatient._subject_ref(body),
            unit=unit_code, nurse=nurse, acuity=acuity,
            assigned_by=user.username)
        session.add(existing)
    else:
        existing.nurse = nurse
        existing.acuity = acuity
        existing.unit = unit_code
        existing.assigned_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="continuum.inpatient.assignment",
                 entity=f"Encounter/{encounter_id}",
                 payload={"nurse": nurse, "acuity": acuity, "unit": unit_code,
                          "patient": existing.patient_ref})
    session.commit()
    return {"encounter_id": encounter_id, "patient_ref": existing.patient_ref,
            "unit": unit_code, "nurse": nurse, "acuity": acuity}


@router.post("/inpatient/assignments/{encounter_id}/unassign")
def unassign(encounter_id: str, session: Session = Depends(get_db),
             user: User = Depends(require_capability(WORK))):
    row = session.scalars(select(NurseAssignment).where(
        NurseAssignment.encounter_id == encounter_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"No assignment for Encounter/{encounter_id}")
    session.delete(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="continuum.inpatient.unassign",
                 entity=f"Encounter/{encounter_id}",
                 payload={"nurse": row.nurse})
    session.commit()
    return {"encounter_id": encounter_id, "unassigned": True}


@router.get("/inpatient/rounds")
def rounds_list(unit: str = Query(...), session: Session = Depends(get_db),
                user: User = Depends(require_capability(READ))):
    """Structured rounds list for one unit: every admitted patient in
    the unit with bed, acuity/nurse, length of stay, discharge plan
    target and barriers — the round walks live data, not a printout."""
    bed_units = _bed_unit_map(session)
    assignments = {a.encounter_id: a for a in
                   session.scalars(select(NurseAssignment)).all()}
    items = []
    now = _now()
    for row in _active_admissions(session):
        body = row.body or {}
        if _admission_unit(session, body, bed_units) != unit:
            continue
        plan = inpatient._plan_of(body) or {}
        assignment = assignments.get(row.resource_id)
        items.append({
            "encounter_id": row.resource_id,
            "patient_ref": inpatient._subject_ref(body),
            "bed": inpatient._current_bed(body),
            "reason": ((body.get("reasonCode") or [{}])[0].get("text")
                       if body.get("reasonCode") else None),
            **inpatient._los(row, now),
            "nurse": assignment.nurse if assignment else None,
            "acuity": assignment.acuity if assignment else None,
            "target_date": plan.get("targetDate"),
            "disposition": plan.get("disposition"),
            "barriers": plan.get("barriers") or [],
            # Depth pass (Workstream 2): today's rounds checklist fact.
            "rounds_checklist_today": _form_response_today(
                session, "rounds-checklist", row.resource_id, now),
        })
    items.sort(key=lambda i: i["bed"] or "")
    return {"unit": unit, "items": items, "total": len(items)}


@router.post("/inpatient/rounds-checklist", status_code=201)
async def record_rounds_checklist(request: Request,
                                  session: Session = Depends(get_db),
                                  user: User = Depends(require_capability(WORK))):
    """Record the interdisciplinary rounds checklist for an ACTIVE
    admission (governed form; required items enforced by the form
    machinery — the teach-back endpoint's pattern). A finished or
    unknown encounter cannot be rounded on."""
    payload = await _json_body(request)
    encounter_id = _require_str(payload, "encounter_id")
    row = inpatient._inpatient_row(session, encounter_id)
    body = row.body or {}
    if body.get("status") != "in-progress":
        raise HTTPException(
            status_code=409,
            detail=f"Rounds checklists need an active (in-progress) "
                   f"admission; Encounter/{encounter_id} is "
                   f"'{body.get('status')}'")
    patient_ref = inpatient._subject_ref(body)
    stored = documentation._store_response(
        session, user, "rounds-checklist", patient_ref,
        payload.get("answers"), encounter_ref=f"Encounter/{encounter_id}")
    append_audit(session, actor=user.username,
                 action="continuum.inpatient.rounds-checklist",
                 entity=f"Encounter/{encounter_id}",
                 payload={"response_id": stored["id"], "patient": patient_ref})
    session.commit()
    return {"response": stored, "form": "rounds-checklist"}


@router.post("/inpatient/discharge-med-rec", status_code=201)
async def record_discharge_med_rec(request: Request,
                                   session: Session = Depends(get_db),
                                   user: User = Depends(require_capability(WORK))):
    """Record the discharge medication reconciliation for an admission
    (governed form; the readiness board picks it up as medrec_done —
    the teach-back endpoint's pattern, one section over)."""
    payload = await _json_body(request)
    encounter_id = _require_str(payload, "encounter_id")
    row = inpatient._inpatient_row(session, encounter_id)
    patient_ref = inpatient._subject_ref(row.body or {})
    stored = documentation._store_response(
        session, user, "discharge-med-rec", patient_ref,
        payload.get("answers"), encounter_ref=f"Encounter/{encounter_id}")
    append_audit(session, actor=user.username,
                 action="continuum.inpatient.discharge-med-rec",
                 entity=f"Encounter/{encounter_id}",
                 payload={"response_id": stored["id"], "patient": patient_ref})
    session.commit()
    return {"response": stored, "form": "discharge-med-rec"}


def _readiness(session: Session, row: FhirResource, now: datetime) -> dict:
    body = row.body or {}
    plan = inpatient._plan_of(body) or {}
    patient_ref = inpatient._subject_ref(body)
    checklist_done = bool(plan.get("checklistResponseId"))
    teachback_done = _form_response_for(
        session, "discharge-teachback", row.resource_id) is not None
    followup = _future_appointment(session, patient_ref) is not None
    # Depth pass (Workstream 2): the fourth readiness element —
    # medication reconciliation. It is always REPORTED (medrec_done);
    # it only joins `missing` (and so only gates `ready`) when the
    # published continuum-ops guard dischargeGuards.requireMedRec is
    # true. The guard ships FALSE, so default discharge behaviour is
    # unchanged — the follow-up guard's config-gated pattern.
    medrec_done = _form_response_for(
        session, "discharge-med-rec", row.resource_id) is not None
    guards = _ops(session).get("dischargeGuards") or {}
    medrec_required = guards.get("requireMedRec") is True
    target = plan.get("targetDate")
    days_to_target = None
    if isinstance(target, str):
        try:
            days_to_target = (date.fromisoformat(target) - now.date()).days
        except ValueError:
            days_to_target = None
    missing = []
    if not checklist_done:
        missing.append("discharge-checklist")
    if not teachback_done:
        missing.append("teach-back")
    if not followup:
        missing.append("follow-up-appointment")
    if medrec_required and not medrec_done:
        missing.append("medication-reconciliation")
    return {"encounter_id": row.resource_id, "patient_ref": patient_ref,
            "bed": inpatient._current_bed(body),
            "target_date": target, "days_to_target": days_to_target,
            "disposition": plan.get("disposition"),
            "barriers": plan.get("barriers") or [],
            "checklist_done": checklist_done,
            "teachback_done": teachback_done,
            "followup_booked": followup,
            "medrec_done": medrec_done,
            "medrec_required": medrec_required,
            "missing": missing, "ready": not missing}


@router.get("/inpatient/discharge-readiness")
def discharge_readiness(session: Session = Depends(get_db),
                        user: User = Depends(require_capability(READ))):
    """The discharge-readiness board (also the data behind the
    published `discharge-readiness` dashboard config): every active
    admission with its expected discharge date, barriers, and the
    readiness facts — checklist submitted, teach-back signed,
    follow-up booked, plus medication reconciliation (medrec_done,
    which gates `ready` only when the published requireMedRec guard
    is on). Discharge planning starts at admission, so
    patients without a plan yet are on the board too, flagged."""
    now = _now()
    bed_units = _bed_unit_map(session)
    items = []
    for row in _active_admissions(session):
        entry = _readiness(session, row, now)
        entry["unit"] = _admission_unit(session, row.body or {}, bed_units)
        entry["has_plan"] = inpatient._plan_of(row.body or {}) is not None
        items.append(entry)
    items.sort(key=lambda i: (i["target_date"] or "9999", i["encounter_id"]))
    ready = sum(1 for i in items if i["ready"])
    return {"items": items, "total": len(items), "ready": ready,
            "generated_at": now.isoformat()}


@router.post("/inpatient/handoffs", status_code=201)
async def create_handoff(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability(WORK))):
    """Record a structured handoff (I-PASS) on an admission: a
    QuestionnaireResponse against the governed handoff form — the
    Joint Commission's standardised-tool expectation, enforced by
    the form's required items."""
    payload = await _json_body(request)
    encounter_id = _require_str(payload, "encounter_id")
    row = inpatient._inpatient_row(session, encounter_id)
    patient_ref = inpatient._subject_ref(row.body or {})
    stored = documentation._store_response(
        session, user, "handoff-ipass", patient_ref, payload.get("answers"),
        encounter_ref=f"Encounter/{encounter_id}")
    append_audit(session, actor=user.username, action="continuum.handoff.create",
                 entity=f"Encounter/{encounter_id}",
                 payload={"response_id": stored["id"], "patient": patient_ref,
                          "to": payload.get("to")})
    session.commit()
    return {"response": stored, "form": "handoff-ipass"}


@router.get("/inpatient/handoffs")
def list_handoffs(encounter_id: str | None = Query(default=None),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "QuestionnaireResponse")
        .order_by(FhirResource.created_at.desc(), FhirResource.id.desc())).all()
    items = []
    for row in rows:
        body = row.body or {}
        if body.get("questionnaire") != "handoff-ipass":
            continue
        enc_ref = ((body.get("encounter") or {}).get("reference") or "")
        if encounter_id and enc_ref != f"Encounter/{encounter_id}":
            continue
        items.append({"id": row.resource_id, "encounter_id":
                      enc_ref.split("/", 1)[1] if "/" in enc_ref else None,
                      "patient_ref": row.patient_ref,
                      "authored": body.get("authored"),
                      "author": (body.get("author") or {}).get("display"),
                      "items": body.get("item") or []})
    return {"items": items, "total": len(items)}


# --- EVS turnaround ---------------------------------------------------------

def _evs_out(row: EvsTask) -> dict:
    turnaround = None
    if row.completed_at and row.released_at:
        turnaround = max(0, int((_aware(row.completed_at)
                                 - _aware(row.released_at)).total_seconds() // 60))
    return {"id": row.task_id, "bed_code": row.bed_code,
            "encounter_id": row.encounter_id, "status": row.status,
            "released_at": _aware(row.released_at).isoformat()
            if row.released_at else None,
            "started_at": _aware(row.started_at).isoformat()
            if row.started_at else None,
            "completed_at": _aware(row.completed_at).isoformat()
            if row.completed_at else None,
            "completed_by": row.completed_by,
            "turnaround_minutes": turnaround,
            "waiting_minutes": (max(0, int((_now() - _aware(row.released_at))
                                           .total_seconds() // 60))
                                if row.status != "completed"
                                and row.released_at else None)}


@router.get("/inpatient/evs")
def evs_board(status: str | None = Query(default=None),
              session: Session = Depends(get_db),
              user: User = Depends(require_capability(READ))):
    """EVS turnaround board: beds released to cleaning and how long
    each turnaround took (or has taken so far). Tasks are created by
    the bed-status path itself — discharge/transfer flips create
    them, the available flip closes them — and tracked against the
    command centre's open bed requests."""
    stmt = select(EvsTask).order_by(EvsTask.released_at.asc(), EvsTask.id.asc())
    if status:
        stmt = stmt.where(EvsTask.status == status)
    rows = session.scalars(stmt).all()
    items = [_evs_out(r) for r in rows]
    open_items = [i for i in items if i["status"] != "completed"]
    done = [i["turnaround_minutes"] for i in items
            if i["turnaround_minutes"] is not None]
    return {"items": items, "total": len(items), "open": len(open_items),
            "avg_turnaround_minutes": (round(sum(done) / len(done), 1)
                                       if done else None)}


def _evs_row(session: Session, task_id: str) -> EvsTask:
    row = session.scalars(select(EvsTask).where(
        EvsTask.task_id == task_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"EVS task {task_id} not found")
    return row


@router.post("/inpatient/evs/{task_id}/start")
def evs_start(task_id: str, session: Session = Depends(get_db),
              user: User = Depends(require_capability(WORK))):
    row = _evs_row(session, task_id)
    if row.status == "completed":
        raise HTTPException(status_code=409, detail="EVS task already completed")
    row.status = "in-progress"
    row.started_at = row.started_at or _now()
    session.flush()
    append_audit(session, actor=user.username, action="continuum.evs.start",
                 entity=f"Location/{row.bed_code}",
                 payload={"task_id": task_id})
    session.commit()
    return _evs_out(row)


@router.post("/inpatient/evs/{task_id}/complete")
def evs_complete(task_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability(WORK))):
    row = _evs_row(session, task_id)
    if row.status == "completed":
        raise HTTPException(status_code=409, detail="EVS task already completed")
    row.status = "completed"
    row.completed_at = _now()
    row.completed_by = user.username
    session.flush()
    append_audit(session, actor=user.username, action="continuum.evs.complete",
                 entity=f"Location/{row.bed_code}",
                 payload={"task_id": task_id,
                          "turnaround_minutes": _evs_out(row)["turnaround_minutes"]})
    session.commit()
    return _evs_out(row)


# ---------------------------------------------------------------------------
# Outpatient operations: clinic sessions, rooming, no-show, backfill
# ---------------------------------------------------------------------------

def _appointment_session(body: dict) -> str | None:
    ext = boards._get_extension(body or {}, SESSION_EXTENSION)
    return ext.get("valueString") if ext else None


def _appointments_on(session: Session, day: date) -> list[FhirResource]:
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Appointment")
        .order_by(FhirResource.start_at.asc(), FhirResource.id.asc())).all()
    return [r for r in rows if r.start_at is not None
            and r.start_at.date() == day]


@router.get("/clinic/templates")
def clinic_templates(session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    """Schedule templates as published: session definitions with
    slot length, capacity and the overbooking limit the booking
    endpoint enforces."""
    ops = _ops(session)
    return {"sessions": ops.get("clinicSessions") or [],
            "total": len(ops.get("clinicSessions") or [])}


@router.get("/clinic/day")
def clinic_day(day: str | None = Query(default=None, alias="date"),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability(READ))):
    """Clinic day view: per session, the booked list against
    capacity; the rooming queue (arrived / checked-in, with rooming
    state); and the day's no-shows and cancellations (backfill
    material)."""
    ops = _ops(session)
    target = date.fromisoformat(day) if day else _now().date()
    appointments = _appointments_on(session, target)
    sessions = []
    for template in ops.get("clinicSessions") or []:
        if not isinstance(template, dict):
            continue
        booked = [a for a in appointments
                  if _appointment_session(a.body or {}) == template.get("code")
                  and a.status not in ("cancelled", "noshow")]
        sessions.append({**template, "booked": len(booked),
                         "overbooked": max(0, len(booked)
                                           - (template.get("capacity") or 0))})
    rooming = []
    for row in appointments:
        if row.status not in ("arrived", "checked-in"):
            continue
        rooming_ext = boards._get_extension(row.body or {}, ROOMING_EXTENSION)
        rooming.append({"appointment_id": row.resource_id,
                        "patient_ref": _appointment_patient_ref(row.body or {}),
                        "start": row.start_at.isoformat() if row.start_at else None,
                        "status": row.status,
                        "rooming": rooming_ext.get("valueString")
                        if rooming_ext else None})
    # Arrivals (UX audit F-1): the day's booked appointments that
    # have not checked in yet — the front desk's check-in worklist.
    # Before this field existed the booked list was invisible on the
    # clinic day view, so the register → book → check-in journey had
    # no last step anywhere in the front desk's own flow.
    arrivals = [{"appointment_id": row.resource_id,
                 "patient_ref": _appointment_patient_ref(row.body or {}),
                 "start": row.start_at.isoformat() if row.start_at else None,
                 "status": row.status,
                 "session": _appointment_session(row.body or {})}
                for row in appointments
                if row.status in ("booked", "proposed", "pending")]
    arrivals.sort(key=lambda a: a["start"] or "")
    return {"date": target.isoformat(), "sessions": sessions,
            "arrivals": arrivals,
            "rooming_queue": rooming,
            "appointments_total": len(appointments)}


def _book_user(session: Session = Depends(get_db),
               user: User = Depends(get_current_user)) -> User:
    """Booking gate (Front Door pass): booking an appointment is
    registration-desk work as much as clinical work, so this ONE
    endpoint also accepts the `registration.work` capability (the
    front desk's). Every other continuum write keeps the plain
    continuum.work gate — rooming, triage and clinical operations do
    not widen."""
    if role_allowed(session, user.role, WORK) \
            or role_allowed(session, user.role, "registration.work"):
        return user
    raise HTTPException(
        status_code=403,
        detail=f"Role {user.role.value} lacks capability '{WORK}' "
               f"or 'registration.work'")


@router.post("/clinic/book", status_code=201)
async def clinic_book(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(_book_user)):
    """Book into a clinic session template. The slot time is the
    template's start + slot length x booked count; booking refuses
    (409) once capacity + the template's overbooking limit is
    reached — overbooking is a configured allowance, never an
    accident."""
    payload = await _json_body(request)
    ops = _ops(session)
    template = _session_template(ops, _require_str(payload, "session"))
    if template is None:
        raise HTTPException(status_code=422,
                            detail="Unknown clinic session: not in the "
                                   "published continuum-ops templates")
    patient_ref = inpatient._patient_ref_of(session, payload)
    day_raw = payload.get("date")
    try:
        day = date.fromisoformat(day_raw) if day_raw else _now().date()
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="'date' must be YYYY-MM-DD")
    existing = [a for a in _appointments_on(session, day)
                if _appointment_session(a.body or {}) == template["code"]
                and a.status not in ("cancelled", "noshow")]
    capacity = template.get("capacity") or 0
    limit = capacity + (template.get("overbookLimit") or 0)
    if len(existing) >= limit:
        raise HTTPException(status_code=409,
                            detail=f"Session {template['code']} on "
                                   f"{day.isoformat()} is full ({len(existing)}"
                                   f"/{limit} incl. overbooking limit)")
    start_clock = time.fromisoformat(template.get("startTime") or "09:00")
    start_dt = datetime.combine(day, start_clock, tzinfo=timezone.utc) \
        + timedelta(minutes=(template.get("slotMinutes") or 20) * len(existing))
    end_dt = start_dt + timedelta(minutes=template.get("slotMinutes") or 20)
    body = {"resourceType": "Appointment", "id": str(uuid.uuid4()),
            "status": "booked",
            "start": start_dt.isoformat(), "end": end_dt.isoformat(),
            "appointmentType": {"text": template.get("display")},
            "participant": [{"actor": {"reference": patient_ref},
                             "status": "accepted"}]}
    boards._set_extension(body, SESSION_EXTENSION,
                          valueString=template["code"])
    created = fhir.create_resource("Appointment", body, session, user)
    overbooked = len(existing) >= capacity
    append_audit(session, actor=user.username, action="continuum.clinic.book",
                 entity=f"Appointment/{created['id']}",
                 payload={"patient": patient_ref, "session": template["code"],
                          "date": day.isoformat(), "overbooked": overbooked})
    session.commit()
    return {"appointment": created, "session": template["code"],
            "overbooked": overbooked}


def _checkin_user(session: Session = Depends(get_db),
                  user: User = Depends(get_current_user)) -> User:
    """Check-in gate (UX audit F-1): marking a booked patient
    arrived is registration-desk work, exactly like booking — this
    ONE endpoint also accepts `registration.work`. Rooming and the
    other continuum writes keep the plain continuum.work gate: the
    front desk checks patients in, the clinical team rooms them."""
    if role_allowed(session, user.role, WORK) \
            or role_allowed(session, user.role, "registration.work"):
        return user
    raise HTTPException(
        status_code=403,
        detail=f"Role {user.role.value} lacks capability '{WORK}' "
               f"or 'registration.work'")


@router.post("/clinic/appointments/{appointment_id}/check-in")
async def clinic_check_in(appointment_id: str,
                          session: Session = Depends(get_db),
                          user: User = Depends(_checkin_user)):
    """Check a booked patient in: Appointment booked → arrived
    (UX audit F-1 — the front-desk journey's missing last step).
    The patient then appears in the rooming queue for the clinical
    team's rooming step. Only a booked-state appointment can check
    in (409 otherwise); the transition is audited."""
    row = fhir._get_row("Appointment", appointment_id, session)
    if row.status not in ("booked", "proposed", "pending"):
        raise HTTPException(status_code=409,
                            detail=f"Appointment {appointment_id} is "
                                   f"'{row.status}'; only a booked "
                                   f"appointment can check in")
    prior_status = row.status
    body = dict(row.body or {})
    body["status"] = "arrived"
    row.body = body
    row.status = "arrived"
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username,
                 action="continuum.clinic.check-in",
                 entity=f"Appointment/{appointment_id}",
                 payload={"patient": _appointment_patient_ref(body),
                          "from_status": prior_status})
    session.commit()
    return {"appointment_id": appointment_id, "status": "arrived",
            "patient": _appointment_patient_ref(body)}


@router.post("/clinic/appointments/{appointment_id}/room")
async def clinic_room(appointment_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability(WORK))):
    """Room a patient: record the room + time on the appointment
    (the rooming queue's working state)."""
    payload = await _json_body(request)
    room = _require_str(payload, "room")
    row = fhir._get_row("Appointment", appointment_id, session)
    if row.status in ("fulfilled", "cancelled", "noshow"):
        raise HTTPException(status_code=409,
                            detail=f"Appointment {appointment_id} is "
                                   f"'{row.status}'; it cannot be roomed")
    import json as _json
    body = dict(row.body or {})
    boards._set_extension(body, ROOMING_EXTENSION, valueString=_json.dumps(
        {"room": room, "roomedAt": _now_iso(), "by": user.username}))
    row.body = body
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="continuum.clinic.room",
                 entity=f"Appointment/{appointment_id}",
                 payload={"room": room,
                          "patient": _appointment_patient_ref(body)})
    session.commit()
    return {"appointment_id": appointment_id, "room": room,
            "roomed_at": _now_iso()}


def _patient_appointment_history(session: Session,
                                 patient_ref: str) -> tuple[int, int]:
    """(attended, missed) past appointments for one patient — the
    deterministic basis of the no-show flag."""
    now_naive = _now().replace(tzinfo=None)
    attended = missed = 0
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Appointment")).all():
        if _appointment_patient_ref(row.body or {}) != patient_ref:
            continue
        if row.start_at is None or row.start_at >= now_naive:
            continue
        if row.status == "noshow":
            missed += 1
        elif row.status == "fulfilled":
            attended += 1
    return attended, missed


@router.get("/clinic/no-show-risks")
def no_show_risks(day: str | None = Query(default=None, alias="date"),
                  session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    """Today's (or the given day's) booked patients with their own
    historical miss rate: attended vs no-show over past appointments.
    The flag is arithmetic over the patient's record against config
    thresholds — deterministic, and labelled as such."""
    ops = _ops(session)
    cfg = ops.get("noShowRisk") or {}
    min_history = cfg.get("minHistory") or 3
    threshold = cfg.get("highRiskMissRate") or 0.34
    target = date.fromisoformat(day) if day else _now().date()
    items = []
    for row in _appointments_on(session, target):
        if row.status not in ("booked", "proposed", "pending"):
            continue
        patient_ref = _appointment_patient_ref(row.body or {})
        if not patient_ref:
            continue
        attended, missed = _patient_appointment_history(session, patient_ref)
        total = attended + missed
        rate = round(missed / total, 3) if total else 0.0
        items.append({"appointment_id": row.resource_id,
                      "patient_ref": patient_ref,
                      "start": row.start_at.isoformat() if row.start_at else None,
                      "history_attended": attended, "history_missed": missed,
                      "miss_rate": rate,
                      "high_risk": bool(total >= min_history
                                         and rate >= threshold),
                      "basis": "patient's own appointment history "
                               "(deterministic count, no model)"})
    items.sort(key=lambda i: (not i["high_risk"], -i["miss_rate"]))
    return {"date": target.isoformat(), "items": items,
            "total": len(items),
            "high_risk": sum(1 for i in items if i["high_risk"])}


@router.get("/clinic/backfill")
def backfill_worklist(session: Session = Depends(get_db),
                      user: User = Depends(require_capability(READ))):
    """Backfill worklist: slots freed in the next 7 days (cancelled /
    no-show appointments) beside the waiting list, with a suggested
    match only where the waitlist slot type names the freed session —
    staff make the offer through the Phase 7 waitlist flow."""
    now = _now()
    horizon = now + timedelta(days=7)
    freed = []
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Appointment")).all():
        if row.status not in ("cancelled", "noshow"):
            continue
        if row.start_at is None:
            continue
        start = row.start_at.replace(tzinfo=timezone.utc)
        if not (now <= start <= horizon):
            continue
        freed.append({"appointment_id": row.resource_id,
                      "start": start.isoformat(),
                      "session": _appointment_session(row.body or {}),
                      "type_code": _coding_code(row.body or {},
                                                "appointmentType"),
                      "patient_ref": _appointment_patient_ref(row.body or {})})
    waiting = [{"entry_id": w.entry_id, "patient_ref": w.patient_ref,
                "slot_type": w.slot_type, "priority": w.priority,
                "status": w.status,
                "created_at": _aware(w.created_at).isoformat()
                if w.created_at else None}
               for w in session.scalars(select(WaitlistEntry).where(
                   WaitlistEntry.status.in_(["waiting", "offered"]))
                   .order_by(WaitlistEntry.priority, WaitlistEntry.id)).all()]
    # A suggested match uses the Phase 7 offer flow's own basis: the
    # waitlist slot type names the freed appointment's type (or its
    # clinic session). Staff still make the actual offer there.
    matches = [{"entry_id": w["entry_id"], "appointment_id": f["appointment_id"]}
               for w in waiting for f in freed
               if w["slot_type"] in (f["session"], f["type_code"])]
    return {"freed_slots": freed, "waiting": waiting, "matches": matches,
            "total": len(freed)}


# ---------------------------------------------------------------------------
# Referral loop closure
# ---------------------------------------------------------------------------

def _referral_stage(status: str, has_report: bool) -> str:
    if status in ("received", "triaged"):
        return "sent"
    if status in ("authorised", "scheduled"):
        return "scheduled"
    if status == "completed":
        return "closed" if has_report else "awaiting-report"
    return status  # denied / cancelled


@router.get("/referrals/loop")
def referral_loop(session: Session = Depends(get_db),
                  user: User = Depends(require_capability(READ))):
    """Referral loop-closure tracking over the Phase 7 referrals:
    sent -> scheduled -> completed -> report-back. The loop is
    CLOSED only when the report-back is recorded; everything else
    ages on this worklist, stale past the configured days."""
    ops = _ops(session)
    stale_days = ops.get("referralStaleDays") or 14
    now = _now()
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "ServiceRequest")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items, stages = [], {}
    for row in rows:
        try:
            access._referral_row(session, row.resource_id)
        except HTTPException:
            continue
        body = row.body or {}
        has_report = boards._get_extension(body, REPORT_BACK_EXTENSION) is not None
        stage = _referral_stage(row.status or "", has_report)
        age_days = max(0, (now - (_aware(row.created_at) or now)).days)
        decorated = access._decorate_referral(session, row)
        items.append({**decorated, "stage": stage,
                      "report_back": has_report, "age_days": age_days,
                      "stale": bool(stage not in ("closed", "denied",
                                                  "cancelled")
                                    and age_days > stale_days)})
        stages[stage] = stages.get(stage, 0) + 1
    items.sort(key=lambda i: -i["age_days"])
    open_items = [i for i in items
                  if i["stage"] not in ("closed", "denied", "cancelled")]
    return {"items": items, "total": len(items), "open": len(open_items),
            "stages": stages}


@router.post("/referrals/{referral_id}/report-back")
async def referral_report_back(referral_id: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability(WORK))):
    """Record the report-back that closes the loop: the referred-to
    service's report has been received and filed."""
    payload = await _json_body(request)
    row = access._referral_row(session, referral_id)
    if row.status != "completed":
        raise HTTPException(status_code=409,
                            detail=f"Report-back needs a completed referral; "
                                   f"this one is '{row.status}'")
    body = dict(row.body or {})
    boards._set_extension(body, REPORT_BACK_EXTENSION,
                          valueDateTime=_now_iso())
    row.body = body
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username,
                 action="continuum.referral.report-back",
                 entity=f"ServiceRequest/{referral_id}",
                 payload={"patient": row.patient_ref,
                          "note": payload.get("note")})
    session.commit()
    return {**access._decorate_referral(session, row), "stage": "closed",
            "report_back": True}


# ---------------------------------------------------------------------------
# Asynchronous inbox (results / messages / tasks / callbacks) with SLAs
# ---------------------------------------------------------------------------

@router.get("/inbox")
def async_inbox(type: str | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability(READ))):
    """One worklist over everything that arrives asynchronously:
    open Tasks, patient message threads awaiting a staff reply,
    critical results awaiting acknowledgement, and discharged-patient
    results awaiting callback. Each item carries its age against the
    per-type SLA from config; breaches sort first."""
    ops = _ops(session)
    sla_map = ops.get("inboxSlaMinutes") or {}
    now = _now()
    items: list[dict] = []

    def add(kind, ref, title, patient_ref, created, extra=None):
        created = _aware(created) or now
        age = max(0, int((now - created).total_seconds() // 60))
        sla = sla_map.get(kind)
        items.append({"type": kind, "ref": ref, "title": title,
                      "patient_ref": patient_ref,
                      "created_at": created.isoformat(),
                      "age_minutes": age, "sla_minutes": sla,
                      "breached": bool(sla and age > sla), **(extra or {})})

    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Task",
            FhirResource.status.in_(["requested", "in-progress",
                                     "accepted"]))).all():
        body = row.body or {}
        add("task", f"Task/{row.resource_id}",
            body.get("description") or "Task", row.patient_ref,
            row.created_at,
            {"task_id": row.resource_id, "status": row.status})
    threads: dict[str, list[FhirResource]] = {}
    for row in engagement._all_messages(session):
        thread_id = engagement._thread_id_of(row.body or {})
        if thread_id:
            threads.setdefault(thread_id, []).append(row)
    for thread_id, rows_ in threads.items():
        rows_.sort(key=lambda r: (r.created_at, r.id))
        latest = rows_[-1]
        if engagement._sender_role_of(latest.body or {}) == "patient":
            add("message", f"thread/{thread_id}",
                "Patient message awaiting reply",
                latest.patient_ref, latest.created_at,
                {"thread_id": thread_id})
    for note in session.scalars(select(LabCriticalNotification).where(
            LabCriticalNotification.state.in_(["pending", "notified"]))).all():
        report = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "DiagnosticReport",
            FhirResource.resource_id == note.report_id)).first()
        add("result", f"DiagnosticReport/{note.report_id}",
            "Critical result awaiting acknowledgement",
            report.patient_ref if report else None, note.created_at,
            {"report_id": note.report_id, "state": note.state})
    for item in _pending_callbacks(session):
        if item["callback_status"] == "completed":
            continue
        add("callback", f"DiagnosticReport/{item['report_id']}",
            "Discharged patient — result callback owed",
            item["patient_ref"], _parse_dt(item["created_at"]),
            {"report_id": item["report_id"]})
    if type:
        items = [i for i in items if i["type"] == type]
    items.sort(key=lambda i: (not i["breached"], -i["age_minutes"]))
    return {"items": items, "total": len(items),
            "breached": sum(1 for i in items if i["breached"])}


@router.post("/tasks/{task_id}/transition")
async def task_transition(task_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    """Work a Task from the inbox / outreach / home-health lists:
    in-progress, completed or cancelled, audited."""
    payload = await _json_body(request)
    target = _require_str(payload, "to_status")
    if target not in ("in-progress", "accepted", "completed", "cancelled"):
        raise HTTPException(status_code=422,
                            detail=f"Unsupported task status '{target}'")
    row = fhir._get_row("Task", task_id, session)
    body = dict(row.body or {})
    current = body.get("status")
    if current in ("completed", "cancelled"):
        raise HTTPException(status_code=409,
                            detail=f"Task {task_id} is already '{current}'")
    body["status"] = target
    if target == "completed":
        body["lastModified"] = _now_iso()
    row.body = body
    row.status = target
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="continuum.task.transition",
                 entity=f"Task/{task_id}",
                 payload={"from_status": current, "to_status": target,
                          "patient": row.patient_ref})
    session.commit()
    return fhir._render(row)


# ---------------------------------------------------------------------------
# Panel operations: care gaps + outreach (Kaiser pattern)
# ---------------------------------------------------------------------------

def _coding_code(body: dict, field: str = "code") -> str | None:
    coding = ((body or {}).get(field) or {}).get("coding") or []
    for entry in coding:
        if isinstance(entry, dict) and entry.get("code"):
            return str(entry["code"])
    return None


def _latest_observation(session: Session, patient_ref: str,
                        code: str) -> tuple[float | None, datetime | None]:
    best_value, best_at = None, None
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Observation",
            FhirResource.patient_ref == patient_ref)).all():
        body = row.body or {}
        if _coding_code(body) != code:
            continue
        quantity = body.get("valueQuantity") or {}
        value = quantity.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        at = _parse_dt(body.get("effectiveDateTime") or body.get("issued")) \
            or _aware(row.created_at)
        if at is not None and (best_at is None or at > best_at):
            best_value, best_at = float(value), at
    return best_value, best_at


def _open_gap_task(session: Session, gap_code: str,
                   patient_ref: str) -> FhirResource | None:
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Task",
            FhirResource.status.in_(["requested", "in-progress",
                                     "accepted"]))).all():
        body = row.body or {}
        if ((body.get("for") or {}).get("reference")) != patient_ref:
            continue
        ext = boards._get_extension(body, "urn:opencare:extension/care-gap")
        if ext and ext.get("valueString") == gap_code:
            return row
    return None


def _gap_items(session: Session, ops: dict) -> list[dict]:
    now = _now()
    items = []
    panels: dict[str, set[str]] = {}
    for gap in ops.get("careGaps") or []:
        if isinstance(gap, dict) and gap.get("conditionCode"):
            panels.setdefault(gap["code"], set())
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Condition")).all():
        code = _coding_code(row.body or {})
        for gap in ops.get("careGaps") or []:
            if isinstance(gap, dict) and gap.get("conditionCode") == code \
                    and row.patient_ref:
                panels[gap["code"]].add(row.patient_ref)
    comparators = {"lt": lambda v, t: v < t, "lte": lambda v, t: v <= t,
                   "gt": lambda v, t: v > t, "gte": lambda v, t: v >= t,
                   "eq": lambda v, t: v == t}
    for gap in ops.get("careGaps") or []:
        if not isinstance(gap, dict):
            continue
        check = gap.get("check") or {}
        compare = comparators.get(check.get("comparator") or "lt")
        for patient_ref in sorted(panels.get(gap["code"], set())):
            value, at = _latest_observation(session, patient_ref,
                                            check.get("code") or "")
            reason = None
            if value is None or at is None:
                reason = "missing"
            elif check.get("withinDays") and \
                    (now - at).days > check["withinDays"]:
                reason = "stale"
            elif compare is not None and check.get("threshold") is not None \
                    and not compare(value, float(check["threshold"])):
                reason = "out-of-range"
            if reason is None:
                continue
            task = _open_gap_task(session, gap["code"], patient_ref)
            items.append({"gap_code": gap["code"],
                          "display": gap.get("display"),
                          "registry": gap.get("registryKey"),
                          "patient_ref": patient_ref, "reason": reason,
                          "latest_value": value,
                          "latest_at": at.isoformat() if at else None,
                          "outreach_task_id": task.resource_id if task else None})
    return items


@router.get("/panels/gaps")
def care_gaps(session: Session = Depends(get_db),
              user: User = Depends(require_capability(READ))):
    """Care-gap worklist per panel: registry patients (by condition)
    whose configured check is missing, stale, or out of range —
    computed from the chart with the same definitions the registry
    measures from. Outreach is a Task; completion is tracked there."""
    ops = _ops(session)
    items = _gap_items(session, ops)
    return {"items": items, "total": len(items),
            "basis": ("deterministic evaluation of the configured gap "
                      "checks over chart conditions + observations")}


@router.post("/panels/gaps/outreach", status_code=201)
async def gap_outreach(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    gap_code = _require_str(payload, "gap_code")
    patient_ref = inpatient._patient_ref_of(session, payload)
    ops = _ops(session)
    gap = next((g for g in ops.get("careGaps") or []
                if isinstance(g, dict) and g.get("code") == gap_code), None)
    if gap is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown care gap '{gap_code}': not in "
                                   "the published continuum-ops config")
    existing = _open_gap_task(session, gap_code, patient_ref)
    if existing is not None:
        return {"task": fhir._render(existing), "existing": True}
    body = {"resourceType": "Task", "id": str(uuid.uuid4()),
            "status": "requested", "intent": "order",
            "description": f"Care gap outreach: {gap.get('display')}",
            "for": {"reference": patient_ref},
            "authoredOn": _now_iso(),
            "requester": {"display": user.username}}
    boards._set_extension(body, "urn:opencare:extension/care-gap",
                          valueString=gap_code)
    created = fhir.create_resource("Task", body, session, user)
    append_audit(session, actor=user.username, action="continuum.panel.outreach",
                 entity=f"Task/{created['id']}",
                 payload={"gap": gap_code, "patient": patient_ref})
    session.commit()
    return {"task": created, "existing": False}


# ---------------------------------------------------------------------------
# Aftercare: teach-back, call queue, home health, readmission, HaH
# ---------------------------------------------------------------------------

@router.post("/aftercare/teach-back", status_code=201)
async def record_teach_back(request: Request, session: Session = Depends(get_db),
                            user: User = Depends(require_capability(WORK))):
    """Record the discharge teach-back sign-off for an admission
    (governed form; the readiness board picks it up)."""
    payload = await _json_body(request)
    encounter_id = _require_str(payload, "encounter_id")
    row = inpatient._inpatient_row(session, encounter_id)
    patient_ref = inpatient._subject_ref(row.body or {})
    stored = documentation._store_response(
        session, user, "discharge-teachback", patient_ref,
        payload.get("answers"), encounter_ref=f"Encounter/{encounter_id}")
    append_audit(session, actor=user.username,
                 action="continuum.aftercare.teach-back",
                 entity=f"Encounter/{encounter_id}",
                 payload={"response_id": stored["id"], "patient": patient_ref})
    session.commit()
    return {"response": stored, "form": "discharge-teachback"}


@router.get("/aftercare/readiness/{encounter_id}")
def aftercare_readiness(encounter_id: str, session: Session = Depends(get_db),
                        user: User = Depends(require_capability(READ))):
    row = inpatient._inpatient_row(session, encounter_id)
    return _readiness(session, row, _now())


@router.get("/aftercare/calls")
def call_queue(session: Session = Depends(get_db),
               user: User = Depends(require_capability(READ))):
    """Post-discharge call queue: every discharge inside the
    configured call window horizon, with its state — scheduled
    (window not yet open), due (inside the 48-72h window) or
    overdue (window passed, no completed call). Derived from the
    A03 event log; completed calls drop off."""
    ops = _ops(session)
    cfg = ops.get("callQueue") or {}
    start_h = cfg.get("windowStartHours") or 48
    end_h = cfg.get("windowEndHours") or 72
    now = _now()
    calls = {c.encounter_id: c for c in
             session.scalars(select(AftercareCall)).all()}
    items = []
    events = session.scalars(select(AdtEvent).where(
        AdtEvent.event_type == "A03").order_by(AdtEvent.created_at.asc())).all()
    for event in events:
        if not event.encounter_id:
            continue
        discharged = _aware(event.created_at) or now
        age_h = (now - discharged).total_seconds() / 3600
        call = calls.get(event.encounter_id)
        if call is not None and call.status == "completed":
            continue
        if age_h < 0:
            continue
        state = ("scheduled" if age_h < start_h
                 else "due" if age_h <= end_h else "overdue")
        items.append({"encounter_id": event.encounter_id,
                      "patient_ref": event.patient_ref,
                      "discharged_at": discharged.isoformat(),
                      "window_opens_at": (discharged + timedelta(hours=start_h)).isoformat(),
                      "window_closes_at": (discharged + timedelta(hours=end_h)).isoformat(),
                      "state": state,
                      "call_status": call.status if call else "not-called"})
    items.sort(key=lambda i: i["discharged_at"])
    return {"items": items, "total": len(items),
            "due": sum(1 for i in items if i["state"] == "due"),
            "overdue": sum(1 for i in items if i["state"] == "overdue"),
            "outcomes": cfg.get("outcomes") or []}


@router.post("/aftercare/calls", status_code=201)
async def record_call(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    encounter_id = _require_str(payload, "encounter_id")
    ops = _ops(session)
    outcomes = (ops.get("callQueue") or {}).get("outcomes") or []
    outcome = payload.get("outcome")
    if outcome is not None and outcomes and outcome not in outcomes:
        raise HTTPException(status_code=422,
                            detail=f"Outcome '{outcome}' is not in the "
                                   "configured call-outcome list")
    event = session.scalars(select(AdtEvent).where(
        AdtEvent.event_type == "A03",
        AdtEvent.encounter_id == encounter_id)).first()
    if event is None:
        raise HTTPException(status_code=422,
                            detail=f"No discharge (A03) is recorded for "
                                   f"Encounter/{encounter_id}")
    row = session.scalars(select(AftercareCall).where(
        AftercareCall.encounter_id == encounter_id)).first()
    if row is None:
        row = AftercareCall(encounter_id=encounter_id,
                            patient_ref=event.patient_ref or "",
                            discharged_at=_aware(event.created_at) or _now())
        session.add(row)
    row.status = "completed"
    row.reached = payload.get("reached") if isinstance(
        payload.get("reached"), bool) else None
    row.outcome = outcome
    row.note = payload.get("note") if isinstance(payload.get("note"), str) else None
    row.called_by = user.username
    row.called_at = _now()
    session.flush()
    append_audit(session, actor=user.username, action="continuum.aftercare.call",
                 entity=f"Encounter/{encounter_id}",
                 payload={"patient": event.patient_ref, "outcome": outcome,
                          "reached": row.reached})
    session.commit()
    return {"encounter_id": encounter_id, "status": "completed",
            "outcome": outcome, "called_by": user.username}


@router.get("/aftercare/home-health")
def home_health_list(session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    """Home-health order tracking: Tasks carrying the home-health
    marker, with their live status."""
    items = []
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Task")
            .order_by(FhirResource.created_at.desc(), FhirResource.id.desc())).all():
        ext = boards._get_extension(row.body or {}, HOME_HEALTH_EXTENSION)
        if ext is None:
            continue
        items.append({"task_id": row.resource_id, "status": row.status,
                      "patient_ref": row.patient_ref,
                      "description": (row.body or {}).get("description"),
                      "service": ext.get("valueString"),
                      "authored": (row.body or {}).get("authoredOn")})
    return {"items": items, "total": len(items)}


@router.post("/aftercare/home-health", status_code=201)
async def home_health_create(request: Request, session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    patient_ref = inpatient._patient_ref_of(session, payload)
    service = _require_str(payload, "service")
    body = {"resourceType": "Task", "id": str(uuid.uuid4()),
            "status": "requested", "intent": "order",
            "description": payload.get("note") or f"Home health: {service}",
            "for": {"reference": patient_ref},
            "authoredOn": _now_iso(),
            "requester": {"display": user.username}}
    if isinstance(payload.get("encounter_id"), str):
        body["focus"] = {"reference": f"Encounter/{payload['encounter_id']}"}
    boards._set_extension(body, HOME_HEALTH_EXTENSION, valueString=service)
    created = fhir.create_resource("Task", body, session, user)
    append_audit(session, actor=user.username,
                 action="continuum.aftercare.home-health",
                 entity=f"Task/{created['id']}",
                 payload={"patient": patient_ref, "service": service})
    session.commit()
    return {"task": created}


# --- Readmission-risk worklist (deterministic, config-weighted) --------------

def _patient_age(session: Session, patient_ref: str,
                 on: date) -> int | None:
    try:
        row = fhir._get_row("Patient", patient_ref.split("/", 1)[1], session)
    except HTTPException:
        return None
    birth = (row.body or {}).get("birthDate")
    try:
        born = date.fromisoformat(birth)
    except (TypeError, ValueError):
        return None
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


@router.get("/aftercare/readmission-risks")
def readmission_risks(session: Session = Depends(get_db),
                      user: User = Depends(require_capability(READ))):
    """Readmission-risk worklist: patients discharged inside the
    configured window whose DETERMINISTIC factor score (weights in
    config; factors named per patient) reaches the flag threshold.
    A triage aid for the aftercare team — the response carries the
    basis string so nobody mistakes it for a validated model."""
    ops = _ops(session)
    cfg = ops.get("readmissionRisk") or {}
    factors_cfg = cfg.get("factors") or {}
    threshold = cfg.get("flagThreshold") or 5
    window = cfg.get("windowDays") or 30
    facility = set(cfg.get("facilityDispositions") or [])
    now = _now()
    finished = [r for r in session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Encounter",
        FhirResource.status == "finished")).all()
        if inpatient._class_code(r.body or {}) == "IMP"]
    items = []
    for row in finished:
        body = row.body or {}
        end = _parse_dt(((body.get("period") or {}).get("end")))
        if end is None or (now - end).days > window or end > now:
            continue
        patient_ref = inpatient._subject_ref(body)
        if not patient_ref:
            continue
        start = _parse_dt(((body.get("period") or {}).get("start")))
        found: dict[str, int] = {}
        age = _patient_age(session, patient_ref, end.date())
        if age is not None and age >= 80 and "age80Plus" in factors_cfg:
            found["age80Plus"] = factors_cfg["age80Plus"]
        elif age is not None and age >= 65 and "age65Plus" in factors_cfg:
            found["age65Plus"] = factors_cfg["age65Plus"]
        if start is not None and (end - start).days >= 7 \
                and "losDays7Plus" in factors_cfg:
            found["losDays7Plus"] = factors_cfg["losDays7Plus"]
        priors = sum(1 for other in finished
                     if other.resource_id != row.resource_id
                     and inpatient._subject_ref(other.body or {}) == patient_ref
                     and _parse_dt(((other.body or {}).get("period") or {})
                                   .get("end")) is not None
                     and 0 <= (end - _parse_dt(((other.body or {})
                                                .get("period") or {})
                                               .get("end"))).days <= 90)
        if priors >= 1 and "priorAdmissions90d" in factors_cfg:
            found["priorAdmissions90d"] = factors_cfg["priorAdmissions90d"]
        conditions = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Condition",
            FhirResource.patient_ref == patient_ref)).all()
        if len(conditions) >= 5 and "conditions5Plus" in factors_cfg:
            found["conditions5Plus"] = factors_cfg["conditions5Plus"]
        meds = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "MedicationRequest",
            FhirResource.patient_ref == patient_ref,
            FhirResource.status == "active")).all()
        if len(meds) >= 10 and "meds10Plus" in factors_cfg:
            found["meds10Plus"] = factors_cfg["meds10Plus"]
        disposition_ext = boards._get_extension(
            body, inpatient.DISCHARGE_DISPOSITION_EXTENSION)
        if disposition_ext and disposition_ext.get("valueString") in facility \
                and "dischargedToFacility" in factors_cfg:
            found["dischargedToFacility"] = factors_cfg["dischargedToFacility"]
        score = sum(found.values())
        if score < threshold:
            continue
        items.append({"encounter_id": row.resource_id,
                      "patient_ref": patient_ref,
                      "discharged_at": end.isoformat(),
                      "score": score, "factors": found})
    items.sort(key=lambda i: -i["score"])
    return {"items": items, "total": len(items), "threshold": threshold,
            "basis": cfg.get("basis")}


# --- Hospital-at-home ---------------------------------------------------------

@router.post("/aftercare/hah/eligibility")
async def hah_eligibility(request: Request, session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    """Set the hospital-at-home eligibility flag on the patient's
    chart (an extension on the Patient record, audited)."""
    payload = await _json_body(request)
    patient_ref = inpatient._patient_ref_of(session, payload)
    eligible = payload.get("eligible")
    if not isinstance(eligible, bool):
        raise HTTPException(status_code=422, detail="'eligible' must be a boolean")
    row = fhir._get_row("Patient", patient_ref.split("/", 1)[1], session)
    body = dict(row.body or {})
    boards._set_extension(body, HAH_ELIGIBLE_EXTENSION, valueBoolean=eligible)
    row.body = body
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="continuum.hah.eligibility",
                 entity=patient_ref,
                 payload={"eligible": eligible,
                          "note": payload.get("note")})
    session.commit()
    return {"patient_ref": patient_ref, "eligible": eligible}


def _hah_eligible_refs(session: Session) -> list[str]:
    refs = []
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Patient")).all():
        ext = boards._get_extension(row.body or {}, HAH_ELIGIBLE_EXTENSION)
        if ext and ext.get("valueBoolean") is True:
            refs.append(f"Patient/{row.resource_id}")
    return refs


def _hah_encounters(session: Session) -> list[FhirResource]:
    return [r for r in session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Encounter",
        FhirResource.status == "in-progress")).all()
        if inpatient._class_code(r.body or {}) == "HH"]


@router.get("/aftercare/hah")
def hah_board(session: Session = Depends(get_db),
              user: User = Depends(require_capability(READ))):
    """Hospital-at-home board: eligible patients and the home census
    (active home-health-class encounters with their open visit
    tasks). Board and workflow only — device/remote-monitoring data
    is Phase 14 and is NOT fabricated here."""
    census = []
    for row in _hah_encounters(session):
        tasks = [t for t in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Task",
            FhirResource.status.in_(["requested", "in-progress",
                                     "accepted"]))).all()
            if ((t.body or {}).get("focus") or {}).get("reference")
            == f"Encounter/{row.resource_id}"]
        census.append({"encounter_id": row.resource_id,
                       "patient_ref": inpatient._subject_ref(row.body or {}),
                       "started_at": ((row.body or {}).get("period") or {})
                       .get("start"),
                       "open_visit_tasks": len(tasks)})
    return {"eligible": _hah_eligible_refs(session),
            "census": census, "total": len(census),
            "note": ("Workflow board only: no device feeds are shown — "
                     "remote monitoring arrives with the Phase 14 "
                     "device platform.")}


@router.post("/aftercare/hah/enroll", status_code=201)
async def hah_enroll(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    patient_ref = inpatient._patient_ref_of(session, payload)
    if patient_ref not in _hah_eligible_refs(session):
        raise HTTPException(status_code=422,
                            detail=f"{patient_ref} is not flagged eligible "
                                   "for hospital-at-home; set the eligibility "
                                   "flag first")
    body = {"resourceType": "Encounter", "id": str(uuid.uuid4()),
            "status": "in-progress",
            "class": {"system": HAH_CLASS_SYSTEM, "code": "HH",
                      "display": "home health"},
            "subject": {"reference": patient_ref},
            "period": {"start": _now_iso()}}
    created = fhir.create_resource("Encounter", body, session, user)
    append_audit(session, actor=user.username, action="continuum.hah.enroll",
                 entity=f"Encounter/{created['id']}",
                 payload={"patient": patient_ref})
    session.commit()
    return {"encounter": created}


@router.post("/aftercare/hah/{encounter_id}/visit-task", status_code=201)
async def hah_visit_task(encounter_id: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    title = _require_str(payload, "title")
    row = fhir._get_row("Encounter", encounter_id, session)
    if inpatient._class_code(row.body or {}) != "HH" \
            or row.status != "in-progress":
        raise HTTPException(status_code=409,
                            detail=f"Encounter/{encounter_id} is not an "
                                   "active hospital-at-home episode")
    body = {"resourceType": "Task", "id": str(uuid.uuid4()),
            "status": "requested", "intent": "order", "description": title,
            "for": {"reference": inpatient._subject_ref(row.body or {})},
            "focus": {"reference": f"Encounter/{encounter_id}"},
            "authoredOn": _now_iso(),
            "requester": {"display": user.username}}
    created = fhir.create_resource("Task", body, session, user)
    append_audit(session, actor=user.username, action="continuum.hah.visit-task",
                 entity=f"Encounter/{encounter_id}",
                 payload={"task_id": created["id"], "title": title})
    session.commit()
    return {"task": created}


@router.post("/aftercare/hah/{encounter_id}/discharge")
def hah_discharge(encounter_id: str, session: Session = Depends(get_db),
                  user: User = Depends(require_capability(WORK))):
    row = fhir._get_row("Encounter", encounter_id, session)
    if inpatient._class_code(row.body or {}) != "HH" \
            or row.status != "in-progress":
        raise HTTPException(status_code=409,
                            detail=f"Encounter/{encounter_id} is not an "
                                   "active hospital-at-home episode")
    body = dict(row.body or {})
    body["status"] = "finished"
    period = dict(body.get("period") or {})
    period["end"] = _now_iso()
    body["period"] = period
    row.body = body
    row.status = "finished"
    row.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="continuum.hah.discharge",
                 entity=f"Encounter/{encounter_id}",
                 payload={"patient": inpatient._subject_ref(body)})
    session.commit()
    return {"encounter": fhir._render(row)}


# ---------------------------------------------------------------------------
# Capacity Command Center
# ---------------------------------------------------------------------------

@router.get("/command/overview")
def command_overview(unit: str | None = Query(default=None),
                     region_id: str | None = Query(default=None),
                     location_id: str | None = Query(default=None),
                     department_id: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability(READ))):
    """One operations surface over live module data: census, ED,
    boarding, bed requests, expected discharges, a DETERMINISTIC
    end-of-day projection (occupied + pending bed requests +
    accepted transfers - expected discharges — arithmetic over
    today's plan, labelled as such, not a forecast model), transport,
    discharges-before-noon, and the diversion/surge banner.

    Optional org-placement scope (Organisation & Registration
    Placement pass): `unit`, `region_id`, `location_id` and
    `department_id` restrict the CENSUS portion (units + totals) to
    the units under the requested placement, resolved through
    app.org_hierarchy.resolve_scope (unknown ids / broken chains
    are a 422 naming the problem). The projection is recomputed
    from the SCOPED totals: its occupied figure and bed denominator
    are scoped, while its boarding / accepted-transfer /
    expected-discharge inputs stay facility-wide because those
    figures are not unit-attributed in the underlying data — the
    response's "projection.basis" says so when scoped, and a
    "scope" block echoes the request + resolved unit codes. ED
    counts, boarding, transport, discharges-today and
    expected_discharges_today likewise remain FACILITY-WIDE in both
    modes (they are not unit-attributed today). With no scope
    parameters the response is exactly the pre-pass shape."""
    census = inpatient.census(session=session, user=user)
    ed = boards.ed_board(session=session, user=user)
    snap = boarding_snapshot(session)
    accepted_transfers = session.scalars(select(TransferRequest).where(
        TransferRequest.status == "accepted")).all()
    totals = census["totals"]
    census_units = census["units"]
    scope = None
    if any(value is not None for value in
           (unit, region_id, location_id, department_id)):
        scope = org_hierarchy.resolve_scope(session, region_id,
                                            location_id, department_id,
                                            unit)
    if scope is not None:
        census_units = [entry for entry in census_units
                        if entry.get("code") in scope]
        scoped_totals: dict = {}
        for entry in census_units:
            for key, value in entry.items():
                if key in ("code", "display", "beds", "occupancy_pct"):
                    continue
                if isinstance(value, (int, float)) \
                        and not isinstance(value, bool):
                    scoped_totals[key] = scoped_totals.get(key, 0) + value
        scoped_total_beds = scoped_totals.get("beds_total", 0)
        scoped_totals["occupancy_pct"] = (
            round(100.0 * scoped_totals.get("occupied", 0)
                  / scoped_total_beds, 1) if scoped_total_beds else 0.0)
        totals = scoped_totals
    expected = census["expected_discharges_today"]["total"]
    projected_occupied = max(0, totals.get("occupied", 0) + snap["total"]
                             + len(accepted_transfers) - expected)
    total_beds = totals.get("beds_total", 0)
    today = _now().date()
    a03_today = [e for e in session.scalars(select(AdtEvent).where(
        AdtEvent.event_type == "A03")).all()
        if _aware(e.created_at) and _aware(e.created_at).date() == today]
    before_noon = sum(1 for e in a03_today
                      if _aware(e.created_at).time() < time(12, 0))
    transport_open = session.scalars(select(TransportRequest).where(
        TransportRequest.status.in_(["requested", "assigned",
                                     "in-transit"]))).all()
    ops_state = _facility_state(session)
    response = {
        "generated_at": _now_iso(),
        "census": {"totals": totals, "units": census_units},
        "expected_discharges_today": census["expected_discharges_today"],
        "ed": {"counts": ed["counts"]},
        "boarding_count": snap["total"],
        "ed_boarding_count": snap["ed_total"],
        "oldest_boarding_minutes": snap["oldest_wait_minutes"],
        "boarding": {"total": snap["total"], "items": snap["items"],
                     "ed_total": snap["ed_total"],
                     "oldest_wait_minutes": snap["oldest_wait_minutes"]},
        "bed_requests": snap["total"] + len(accepted_transfers),
        "accepted_transfers": len(accepted_transfers),
        "projection": {
            "projected_occupied_end_of_day": projected_occupied,
            "projected_occupancy_pct": (round(100.0 * projected_occupied
                                               / total_beds, 1)
                                         if total_beds else 0.0),
            "basis": ("deterministic arithmetic: current occupied + "
                      "pending bed requests + accepted transfers - "
                      "expected discharges today; not a forecast model"),
        },
        "discharges_today": {"total": len(a03_today),
                             "before_noon": before_noon},
        "transport_open": len(transport_open),
        "diversion": ops_state["diversion"],
        "surge_level": ops_state["surge_level"],
    }
    if scope is not None:
        response["scope"] = {"region": region_id,
                             "location": location_id,
                             "department": department_id,
                             "unit": unit,
                             "unit_codes": sorted(scope)}
        response["projection"]["basis"] += (
            f"; SCOPE: census units/totals and the projection's "
            f"occupied figure + bed denominator are restricted to "
            f"units {sorted(scope)} under the requested org "
            f"placement — boarding, accepted transfers and expected "
            f"discharges in the projection (and ED counts, transport "
            f"and expected_discharges_today) remain facility-wide "
            f"because they are not unit-attributed")
    return response


@router.get("/command/bed-requests")
def bed_requests(session: Session = Depends(get_db),
                 user: User = Depends(require_capability(READ))):
    """The bed-request queue the command centre works: admissions
    awaiting a bed (assignable here) + accepted transfers awaiting
    an admission."""
    snap = boarding_snapshot(session)
    items = [{"kind": "admission", **item} for item in snap["items"]]
    for transfer in session.scalars(select(TransferRequest).where(
            TransferRequest.status == "accepted")
            .order_by(TransferRequest.decided_at.asc())).all():
        items.append({"kind": "transfer",
                      "request_id": transfer.request_id,
                      "patient_name": transfer.patient_name,
                      "referring_facility": transfer.referring_facility,
                      "service_line": transfer.service_line,
                      "urgency": transfer.urgency,
                      "admission_task_id": transfer.admission_task_id})
    return {"items": items, "total": len(items)}


@router.post("/command/bed-requests/{encounter_id}/assign")
async def command_assign_bed(encounter_id: str, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability(WORK))):
    """Assign a bed from the command centre — the same act as the
    bed board's assign-bed (same helpers, same A01 event, same bed
    state path), worked from the queue."""
    payload = await _json_body(request)
    row = inpatient._inpatient_row(session, encounter_id)
    body = dict(row.body or {})
    if body.get("status") != "in-progress":
        raise HTTPException(status_code=409,
                            detail=f"Encounter {encounter_id} is "
                                   f"'{body.get('status')}'; bed assignment "
                                   "requires an in-progress admission")
    if inpatient._current_bed(body) is not None:
        raise HTTPException(status_code=409,
                            detail=f"Encounter {encounter_id} already has "
                                   "a bed; use the transfer endpoint to move it")
    concept, bed_row = inpatient._require_available_bed(session,
                                                         payload.get("bed"))
    bed_code = concept["code"]
    inpatient._add_location_entry(body, bed_code, _now_iso())
    row.body = body
    row.version += 1
    session.flush()
    inpatient._occupy_bed(session, user, bed_row, encounter_id,
                          "command-centre assignment")
    patient_ref = inpatient._subject_ref(body)
    event = inpatient._record_event(
        session, user, "A01", encounter_id, patient_ref,
        to_location=bed_code,
        detail={"bed": bed_code, "unit": concept.get("parent"),
                "via": "command-centre"})
    session.commit()
    return {"encounter": fhir._render(row),
            "bed": inpatient._bed_entry(session, concept, _now()),
            "event": inpatient._event_out(session, event)}


# --- Transfer center ----------------------------------------------------------

def _transfer_out(row: TransferRequest) -> dict:
    return {"id": row.request_id, "patient_name": row.patient_name,
            "patient_age": row.patient_age, "patient_sex": row.patient_sex,
            "referring_facility": row.referring_facility,
            "referring_clinician": row.referring_clinician,
            "reason": row.reason, "service_line": row.service_line,
            "urgency": row.urgency, "contact": row.contact,
            "status": row.status, "decision_reason": row.decision_reason,
            "decided_by": row.decided_by,
            "decided_at": _aware(row.decided_at).isoformat()
            if row.decided_at else None,
            "admission_task_id": row.admission_task_id,
            "created_by": row.created_by,
            "created_at": _aware(row.created_at).isoformat()
            if row.created_at else None}


@router.get("/command/transfers")
def list_transfers(status: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability(READ))):
    stmt = select(TransferRequest).order_by(TransferRequest.created_at.asc(),
                                            TransferRequest.id.asc())
    if status:
        stmt = stmt.where(TransferRequest.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_transfer_out(r) for r in rows], "total": len(rows)}


@router.post("/command/transfers", status_code=201)
async def create_transfer(request: Request, session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    """Transfer-centre intake: an external facility's request, with
    the clinical summary the accepting team decides on. The patient
    is external — no chart exists until an admission is created."""
    payload = await _json_body(request)
    row = TransferRequest(
        request_id=str(uuid.uuid4()),
        patient_name=_require_str(payload, "patient_name"),
        patient_age=payload.get("patient_age")
        if isinstance(payload.get("patient_age"), int) else None,
        patient_sex=payload.get("patient_sex"),
        referring_facility=_require_str(payload, "referring_facility"),
        referring_clinician=payload.get("referring_clinician"),
        reason=_require_str(payload, "reason"),
        service_line=payload.get("service_line"),
        urgency=payload.get("urgency") or "routine",
        contact=payload.get("contact"),
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="continuum.transfer.intake",
                 entity=f"transfer/{row.request_id}",
                 payload={"referring_facility": row.referring_facility,
                          "service_line": row.service_line,
                          "urgency": row.urgency})
    session.commit()
    return _transfer_out(row)


@router.post("/command/transfers/{request_id}/decide")
async def decide_transfer(request_id: str, request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability(WORK))):
    """Accept or decline a transfer. Declining REQUIRES a reason.
    Accepting creates the admission Task the bed team works from the
    bed-request queue."""
    payload = await _json_body(request)
    decision = _require_str(payload, "decision")
    if decision not in ("accept", "decline"):
        raise HTTPException(status_code=422,
                            detail="'decision' must be 'accept' or 'decline'")
    row = session.scalars(select(TransferRequest).where(
        TransferRequest.request_id == request_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Transfer request {request_id} not found")
    if row.status != "pending":
        raise HTTPException(status_code=409,
                            detail=f"Transfer request is already '{row.status}'")
    reason = payload.get("reason")
    if decision == "decline" and not (isinstance(reason, str)
                                      and reason.strip()):
        raise HTTPException(status_code=422,
                            detail="Declining a transfer requires a reason")
    row.status = "accepted" if decision == "accept" else "declined"
    row.decision_reason = reason if isinstance(reason, str) else None
    row.decided_by = user.username
    row.decided_at = _now()
    task_id = None
    if decision == "accept":
        task_id = str(uuid.uuid4())
        body = {"resourceType": "Task", "id": task_id, "status": "requested",
                "intent": "order",
                "description": (f"Transfer accepted — arrange admission: "
                                f"{row.patient_name} from "
                                f"{row.referring_facility} "
                                f"({row.service_line or 'general'})"),
                "authoredOn": _now_iso(),
                "requester": {"display": user.username}}
        boards._set_extension(body, "urn:opencare:extension/transfer-request",
                              valueString=request_id)
        created = fhir.create_resource("Task", body, session, user)
        task_id = created["id"]
        row.admission_task_id = task_id
    session.flush()
    append_audit(session, actor=user.username, action="continuum.transfer.decide",
                 entity=f"transfer/{request_id}",
                 payload={"decision": decision, "reason": row.decision_reason,
                          "admission_task_id": task_id})
    session.commit()
    return _transfer_out(row)


# --- Transport dispatch ---------------------------------------------------------

_TRANSPORT_FLOW = {"requested": {"assigned", "cancelled"},
                   "assigned": {"in-transit", "cancelled"},
                   "in-transit": {"completed", "cancelled"},
                   "completed": set(), "cancelled": set()}


def _transport_out(row: TransportRequest) -> dict:
    return {"id": row.request_id, "patient_ref": row.patient_ref,
            "from_location": row.from_location, "to_location": row.to_location,
            "priority": row.priority, "reason": row.reason,
            "status": row.status, "transporter": row.transporter,
            "requested_by": row.requested_by,
            "created_at": _aware(row.created_at).isoformat()
            if row.created_at else None}


@router.get("/command/transport")
def list_transport(session: Session = Depends(get_db),
                   user: User = Depends(require_capability(READ))):
    rows = session.scalars(select(TransportRequest).order_by(
        TransportRequest.created_at.asc(), TransportRequest.id.asc())).all()
    items = [_transport_out(r) for r in rows]
    return {"items": items, "total": len(items),
            "open": sum(1 for i in items
                        if i["status"] in ("requested", "assigned",
                                           "in-transit"))}


@router.post("/command/transport", status_code=201)
async def create_transport(request: Request, session: Session = Depends(get_db),
                           user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    patient_ref = inpatient._patient_ref_of(session, payload)
    row = TransportRequest(
        request_id=str(uuid.uuid4()), patient_ref=patient_ref,
        from_location=_require_str(payload, "from_location"),
        to_location=_require_str(payload, "to_location"),
        priority=payload.get("priority") or "routine",
        reason=payload.get("reason"), requested_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="continuum.transport.create",
                 entity=f"transport/{row.request_id}",
                 payload={"patient": patient_ref, "from": row.from_location,
                          "to": row.to_location})
    session.commit()
    return _transport_out(row)


@router.post("/command/transport/{request_id}/transition")
async def transport_transition(request_id: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability(WORK))):
    payload = await _json_body(request)
    target = _require_str(payload, "to_status")
    row = session.scalars(select(TransportRequest).where(
        TransportRequest.request_id == request_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Transport request {request_id} not found")
    if target not in _TRANSPORT_FLOW.get(row.status, set()):
        raise HTTPException(status_code=409,
                            detail=f"Illegal transport transition "
                                   f"'{row.status}' -> '{target}'")
    row.status = target
    if isinstance(payload.get("transporter"), str):
        row.transporter = payload["transporter"]
    session.flush()
    append_audit(session, actor=user.username,
                 action="continuum.transport.transition",
                 entity=f"transport/{request_id}",
                 payload={"to_status": target, "transporter": row.transporter})
    session.commit()
    return _transport_out(row)


# ---------------------------------------------------------------------------
# Safety & quality operations (Armstrong Institute pattern)
# ---------------------------------------------------------------------------

def _reporter_display(event: SafetyEvent, user: User) -> str:
    if event.confidential and user.username != event.reporter:
        return "confidential"
    return event.reporter


def _event_out(session: Session, event: SafetyEvent, user: User) -> dict:
    return {"id": event.event_id,
            "reporter": _reporter_display(event, user),
            "patient_ref": event.patient_ref, "unit": event.unit,
            "category": event.category, "harm_level": event.harm_level,
            "description": event.description,
            "confidential": event.confidential, "status": event.status,
            "rca_required": event.rca_required,
            "rca_status": event.rca_status,
            "occurred_at": _aware(event.occurred_at).isoformat()
            if event.occurred_at else None,
            "created_at": _aware(event.created_at).isoformat()
            if event.created_at else None}


@router.post("/safety/events", status_code=201)
async def file_safety_event(request: Request, session: Session = Depends(get_db),
                            user: User = Depends(require_capability("safety.report"))):
    """File a safety event report. ANY staff role may file. Category
    and harm level are the published taxonomy, not code. Serious
    harm (config list) automatically opens an RCA work item."""
    payload = await _json_body(request)
    ops = _ops(session)
    description = _require_str(payload, "description")
    category = payload.get("category") or "other"
    if category not in (ops.get("safetyEventCategories") or []):
        raise HTTPException(status_code=422,
                            detail=f"Unknown safety category '{category}': "
                                   "not in the published continuum-ops taxonomy")
    harm = payload.get("harm_level") or "none"
    harm_codes = {h.get("code") for h in ops.get("harmTaxonomy") or []
                  if isinstance(h, dict)}
    if harm not in harm_codes:
        raise HTTPException(status_code=422,
                            detail=f"Unknown harm level '{harm}': not in "
                                   "the published continuum-ops taxonomy")
    patient_ref = None
    if payload.get("patient"):
        patient_ref = inpatient._patient_ref_of(session, payload)
    rca = harm in (ops.get("rcaHarmLevels") or [])
    event = SafetyEvent(event_id=str(uuid.uuid4()), reporter=user.username,
                        patient_ref=patient_ref,
                        unit=payload.get("unit")
                        if isinstance(payload.get("unit"), str) else None,
                        category=category, harm_level=harm,
                        description=description,
                        confidential=payload.get("confidential") is True,
                        rca_required=rca,
                        rca_status="open" if rca else None,
                        occurred_at=_parse_dt(payload.get("occurred_at")))
    session.add(event)
    session.flush()
    append_audit(session, actor=user.username, action="continuum.safety.report",
                 entity=f"safety-event/{event.event_id}",
                 payload={"category": category, "harm_level": harm,
                          "confidential": event.confidential,
                          "unit": event.unit, "rca_required": rca})
    session.commit()
    return _event_out(session, event, user)


@router.get("/safety/events")
def list_safety_events(status: str | None = Query(default=None),
                       unit: str | None = Query(default=None),
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("safety.review"))):
    stmt = select(SafetyEvent).order_by(SafetyEvent.created_at.desc(),
                                        SafetyEvent.id.desc())
    if status:
        stmt = stmt.where(SafetyEvent.status == status)
    if unit:
        stmt = stmt.where(SafetyEvent.unit == unit)
    rows = session.scalars(stmt).all()
    return {"items": [_event_out(session, r, user) for r in rows],
            "total": len(rows)}


def _safety_row(session: Session, event_id: str) -> SafetyEvent:
    row = session.scalars(select(SafetyEvent).where(
        SafetyEvent.event_id == event_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Safety event {event_id} not found")
    return row


@router.get("/safety/events/{event_id}")
def safety_event_detail(event_id: str, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("safety.report"))):
    """Event detail + the follow-up thread. Safety reviewers see
    every event; a reporter sees their own. A confidential reporter's
    identity is shielded from everyone but themselves."""
    from .configstore import role_allowed
    event = _safety_row(session, event_id)
    if user.username != event.reporter \
            and not role_allowed(session, user.role, "safety.review"):
        raise HTTPException(status_code=403,
                            detail="Only the reporter or a safety "
                                   "reviewer may view this event")
    messages = []
    for msg in session.scalars(select(SafetyEventMessage).where(
            SafetyEventMessage.event_id == event_id)
            .order_by(SafetyEventMessage.created_at.asc(),
                      SafetyEventMessage.id.asc())).all():
        if msg.author_role == "reporter":
            author = _reporter_display(event, user) \
                if user.username != msg.author else msg.author
            if event.confidential and user.username != msg.author:
                author = "reporter (confidential)"
        else:
            author = msg.author
        messages.append({"id": msg.id, "author": author,
                         "author_role": msg.author_role, "body": msg.body,
                         "created_at": _aware(msg.created_at).isoformat()
                         if msg.created_at else None})
    return {**_event_out(session, event, user), "messages": messages}


@router.post("/safety/events/{event_id}/messages", status_code=201)
async def safety_event_message(event_id: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(require_capability("safety.report"))):
    """Two-way follow-up: the reporter and reviewers exchange
    messages on the event. Only the reporter or a safety reviewer
    may post; a confidential reporter's identity stays shielded in
    every view."""
    from .configstore import role_allowed
    payload = await _json_body(request)
    body_text = _require_str(payload, "body")
    event = _safety_row(session, event_id)
    is_reporter = user.username == event.reporter
    if not is_reporter and not role_allowed(session, user.role, "safety.review"):
        raise HTTPException(status_code=403,
                            detail="Only the reporter or a safety reviewer "
                                   "may post follow-up on this event")
    msg = SafetyEventMessage(event_id=event_id, author=user.username,
                             author_role="reporter" if is_reporter
                             else "reviewer", body=body_text)
    session.add(msg)
    session.flush()
    append_audit(session, actor=user.username, action="continuum.safety.message",
                 entity=f"safety-event/{event_id}",
                 payload={"author_role": msg.author_role})
    session.commit()
    return {"id": msg.id, "event_id": event_id,
            "author_role": msg.author_role}


@router.post("/safety/events/{event_id}/triage")
async def safety_event_triage(event_id: str, request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability("safety.review"))):
    """Reviewer triage: move the event through reported -> triaged
    -> in-review -> closed, reclassify harm, require (or clear) an
    RCA, and optionally leave the reporter a follow-up note."""
    payload = await _json_body(request)
    event = _safety_row(session, event_id)
    if event.status == "closed":
        raise HTTPException(status_code=409,
                            detail="This safety event is closed")
    ops = _ops(session)
    if payload.get("status") is not None:
        target = payload["status"]
        if target not in ("triaged", "in-review", "closed"):
            raise HTTPException(status_code=422,
                                detail=f"Unsupported triage status '{target}'")
        event.status = target
        if target == "closed" and event.rca_status == "open":
            event.rca_status = "closed"
    if payload.get("harm_level") is not None:
        harm_codes = {h.get("code") for h in ops.get("harmTaxonomy") or []
                      if isinstance(h, dict)}
        if payload["harm_level"] not in harm_codes:
            raise HTTPException(status_code=422,
                                detail="Unknown harm level")
        event.harm_level = payload["harm_level"]
    if isinstance(payload.get("rca_required"), bool):
        event.rca_required = payload["rca_required"]
        event.rca_status = "open" if payload["rca_required"] else None
    note = payload.get("note")
    if isinstance(note, str) and note.strip():
        session.add(SafetyEventMessage(event_id=event_id,
                                       author=user.username,
                                       author_role="reviewer",
                                       body=note.strip()))
    session.flush()
    append_audit(session, actor=user.username, action="continuum.safety.triage",
                 entity=f"safety-event/{event_id}",
                 payload={"status": event.status,
                          "harm_level": event.harm_level,
                          "rca_required": event.rca_required})
    session.commit()
    return _event_out(session, event, user)


@router.get("/safety/rca")
def safety_rca_queue(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("safety.review"))):
    """RCA workqueue: events flagged for root-cause analysis whose
    RCA is still open."""
    rows = session.scalars(select(SafetyEvent).where(
        SafetyEvent.rca_required.is_(True),
        SafetyEvent.rca_status == "open")
        .order_by(SafetyEvent.created_at.asc(), SafetyEvent.id.asc())).all()
    return {"items": [_event_out(session, r, user) for r in rows],
            "total": len(rows)}


@router.get("/safety/summary")
def safety_summary(unit: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("safety.review"))):
    """Unit safety summary (also the data behind the published
    `unit-safety` dashboard config): volumes by status, harm and
    category, and the open RCA count."""
    stmt = select(SafetyEvent)
    if unit:
        stmt = stmt.where(SafetyEvent.unit == unit)
    rows = session.scalars(stmt).all()
    by_status: dict[str, int] = {}
    by_harm: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_unit: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        by_harm[row.harm_level] = by_harm.get(row.harm_level, 0) + 1
        by_category[row.category] = by_category.get(row.category, 0) + 1
        if row.unit:
            by_unit[row.unit] = by_unit.get(row.unit, 0) + 1
    rca_open = sum(1 for r in rows
                   if r.rca_required and r.rca_status == "open")
    return {"total": len(rows),
            "open": sum(1 for r in rows if r.status != "closed"),
            "rca_open": rca_open, "by_status": by_status,
            "by_harm": by_harm, "by_category": by_category,
            "by_unit": by_unit, "unit": unit}
