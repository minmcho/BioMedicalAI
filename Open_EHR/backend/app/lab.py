"""Phase 2 — Lab LIS (Laboratory Information System).

Order -> specimen -> result -> QC workflow, built ON the Studio
customization engine (docs/STUDIO_CUSTOMIZATION.md): everything
site-specific is published Studio configuration, never hardcoded —

- the orderable catalog (panels, analytes, units, reference ranges and
  critical thresholds) is terminology config, key ``lab-catalog``;
- the order/specimen state machine is workflow config, key
  ``lab-order-flow`` (the constant below is only the seed/fallback);

both seeded as published v1 at startup by :func:`ensure_lab_defaults`,
and editable through the normal /studio/config draft -> publish flow.

Flow and governance:
- Orders are FHIR ServiceRequest resources placed via POST /lab/orders;
  the ordered code must be an ACTIVE concept in the published catalog
  (unknown/inactive -> 422).
- Collection creates a linked FHIR Specimen with a server-generated
  accession id; lifecycle transitions run through the workflow config
  (illegal -> 409, every transition audit-logged).
- Results (manual entry, HL7 v2 ORU^R01 ingest, or CSV ingest) are
  created as PRELIMINARY Observations + a DiagnosticReport, all carrying
  the lab-unreleased visibility tag (app.fhir): they are invisible in the
  clinician inbox, the patient portal, and every patient read path until
  RELEASED. Verification (report preliminary -> final) and release
  (observations -> final, tag removed) are separate audited steps gated
  by the lab.verify capability — nothing is ever auto-released.
- Reference ranges + critical thresholds from the catalog are applied at
  result creation: Observation.interpretation is set to N / L / H / LL /
  HH (HL7 v3 ObservationInterpretation codes) and a report containing a
  critical (LL/HH) value carries a critical-result extension.
- QC runs are first-class records (app.models.LabQcRun) with pass/fail
  computed server-side. If the LATEST QC run for an analyte failed,
  verification and release of any report containing that analyte are
  blocked (409) until a passing QC run exists after it.

Ranges in v1 are age/sex-agnostic (one range per analyte from config) —
a stated limitation, see docs/sqa/PHASE2_SQA_REPORT.md.

Phase 6B (full clinical laboratory, roadmap §7) builds ON this spine
without rewriting it — see docs/sqa/PHASE6B_SQA_REPORT.md:

- Pre-analytical: a collection worklist (ordered orders grouped by
  patient/location, priority order from catalog config), chain-of-custody
  timestamps on the specimen (collected / received / in-analysis), and
  coded specimen-rejection criteria (terminology config) with a
  recollection workflow — rejection records a structured history entry
  on the order and spawns a recollect Task; POST /lab/orders/{id}/recollect
  returns the order to `ordered` with the rejected specimen preserved.
- Bench catalogues: coagulation + urinalysis benches join the published
  catalog (qualitative/coded results for concepts flagged `qualitative`
  in config — the numeric path is unchanged), plus microbiology,
  blood-bank and pathology orderable concepts whose `bench` field routes
  the order to its bench workflow (app.lab_benches runs those benches).
- Post-analytical: auto-verification (config rules; verifies, NEVER
  releases; audited as actor "system:auto-verify"), delta checks flagged
  on the verification queue, reflex rules creating flagged draft orders,
  and amended reports (new linked version, original preserved).
- QC programme: control levels + Westgard-style flags computed at run
  creation from the published programme config, Levey-Jennings data API.
"""
import csv
import io
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .hl7 import _hl7_component, _hl7_field, parse_hl7_oru  # noqa: F401
# The HL7 v2 reader lives in app.hl7 (the ONE shared HL7 module) since
# Phase 8; it is re-exported here so Phase 2 imports/tests are
# untouched. (parse_hl7_oru + its field/component helpers moved
# verbatim — see app.hl7.)
from .configstore import (load_workflow_machine, machine_from_payload,
                          payload_state_set, published_config,
                          transition_role_allowed, workflow_payload)
from .models import (ConfigStatus, FhirResource, LabCriticalNotification,
                     LabQcRun, Role, StudioConfig, User)
from .security import get_db, get_current_user, require_capability

router = APIRouter(prefix="/lab", tags=["lab"])

LAB_CATALOG_KEY = "lab-catalog"
LAB_WORKFLOW_KEY = "lab-order-flow"
# Phase 5 (deployment profiles, roadmap §5): POCT mode. An order placed
# with `poct: true` carries this extension and runs on the ALTERNATE
# published workflow `lab-order-flow-poct` — a machine WITHOUT the
# verify state, whose result entry verifies + releases in a single
# step (office point-of-care tests). The alternate workflow exists
# only when a deployment profile (or a configurator) publishes it;
# without it, a poct-flagged order falls back to the standard machine
# and the standard two-step path. Hospital orders are never flagged
# and are completely unaffected.
POCT_WORKFLOW_KEY = "lab-order-flow-poct"
POCT_EXTENSION = "urn:opencare:extension/poct"
LOINC = "http://loinc.org"
INTERPRETATION_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
CRITICAL_EXTENSION_URL = "urn:opencare:extension/critical-result"

# Phase 6B bench routing: a catalog concept may declare a `bench`. Two
# benches run their own published workflow state machines on this same
# spine (their bench endpoints live in app.lab_benches); every other
# bench (chemistry, haematology, coagulation, urinalysis, blood-bank)
# runs the standard machine above.
MICRO_WORKFLOW_KEY = "lab-micro-flow"
PATHOLOGY_WORKFLOW_KEY = "lab-pathology-flow"
ORGANISMS_KEY = "micro-organisms"
ANTIBIOTICS_KEY = "micro-antibiotics"
SYNOPTIC_FORM_KEY = "pathology-synoptic"
BENCH_WORKFLOW_KEYS = {"microbiology": MICRO_WORKFLOW_KEY,
                       "pathology": PATHOLOGY_WORKFLOW_KEY}
REFLEX_EXTENSION = "urn:opencare:extension/reflex"
REFLEX_SOURCE_EXTENSION = "urn:opencare:extension/reflex-source-report"
AMENDS_EXTENSION = "urn:opencare:extension/amends-report"
AMENDED_BY_EXTENSION = "urn:opencare:extension/amended-by-report"
AMENDMENT_REASON_EXTENSION = "urn:opencare:extension/amendment-reason"
INSTRUMENT_EXTENSION = "urn:opencare:extension/instrument"
IN_ANALYSIS_EXTENSION = "urn:opencare:extension/in-analysis-at"

# Fallback machines for the bench workflows (the live machines are the
# published configs seeded below; these constants apply only if a bench
# config is disabled). final / signed-out / reported and the staged
# micro states are GOVERNED: reached through bench endpoints, never by
# a bare transition flip.
DEFAULT_MICRO_TRANSITIONS: dict[str, set[str]] = {
    "ordered": {"collected", "cancelled"},
    "collected": {"plated", "rejected"},
    "plated": {"rejected"},
    "preliminary-reported": set(),
    "organism-identified": set(),
    "susceptibility-reported": set(),
    "final": set(),
    "rejected": set(),
    "cancelled": set(),
}
DEFAULT_PATHOLOGY_TRANSITIONS: dict[str, set[str]] = {
    "ordered": {"collected", "cancelled"},
    "collected": {"accessioned", "rejected"},
    "accessioned": {"grossed", "rejected"},
    "grossed": {"histology"},
    "histology": set(),
    "reported": set(),
    "signed-out": set(),
    "rejected": set(),
    "cancelled": set(),
}

# Fallback copy of the lab order state machine — the live machine is the
# published workflow config (seeded v1 below with exactly these
# transitions); this constant is used only if that config is disabled.
# resulted/verified/released are reachable only through the result /
# verify / release endpoints, not the generic transition endpoint.
DEFAULT_LAB_TRANSITIONS: dict[str, set[str]] = {
    "ordered": {"collected", "cancelled"},
    "collected": {"received", "rejected"},
    "received": {"in-process", "rejected"},
    "in-process": {"resulted", "rejected"},
    "resulted": {"verified"},
    "verified": {"released"},
    "released": set(),
    "rejected": set(),
    "cancelled": set(),
}
# States the generic transition endpoint will not move INTO: each has a
# dedicated endpoint that performs the associated work (result creation,
# verification sign-off, release) rather than a bare status flip.
GOVERNED_STATES = ("resulted", "verified", "released")
_SPECIMEN_MIRROR = {"collected", "received", "in-process", "resulted",
                    "verified", "released", "rejected"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Default Studio configuration (seeded published v1 at startup)
# ---------------------------------------------------------------------------

def default_lab_catalog_payload() -> dict:
    def analyte(code, display, unit, low=None, high=None,
                critical_low=None, critical_high=None, bench="chemistry",
                **extra):
        rng = {"unit": unit}
        if low is not None:
            rng["low"] = low
        if high is not None:
            rng["high"] = high
        if critical_low is not None:
            rng["criticalLow"] = critical_low
        if critical_high is not None:
            rng["criticalHigh"] = critical_high
        concept = {"code": code, "display": display, "active": True,
                   "unit": unit, "referenceRange": rng, "bench": bench}
        concept.update(extra)
        return concept

    def qualitative(code, display, values, bench, **extra):
        """A coded/qualitative result concept (Phase 6B): results are
        entered as one of `values` (stored as a valueString), never as
        a number. The numeric validation path is untouched for the
        concepts that do not carry this flag."""
        concept = {"code": code, "display": display, "active": True,
                   "qualitative": True, "values": list(values), "bench": bench}
        concept.update(extra)
        return concept

    concepts = [
        # CBC analytes
        analyte("718-7", "Hemoglobin [Mass/volume] in Blood", "g/dL",
                12.0, 15.5, critical_low=7.0, critical_high=20.0,
                bench="haematology", delta={"pct": 15}),
        analyte("4544-3", "Hematocrit [Volume Fraction] of Blood by Automated count",
                "%", 36.0, 46.0, critical_low=20.0, critical_high=60.0),
        analyte("6690-2", "Leukocytes [#/volume] in Blood by Automated count",
                "10^9/L", 4.0, 11.0, critical_high=30.0),
        analyte("777-3", "Platelets [#/volume] in Blood by Automated count",
                "10^9/L", 150.0, 400.0, critical_low=50.0, critical_high=1000.0),
        # BMP analytes
        analyte("2345-7", "Glucose [Mass/volume] in Serum or Plasma", "mg/dL",
                70.0, 99.0, critical_low=40.0, critical_high=500.0,
                autoVerify={"low": 65.0, "high": 110.0}),
        analyte("3094-0", "Urea nitrogen [Mass/volume] in Serum or Plasma", "mg/dL",
                7.0, 20.0, critical_high=100.0),
        analyte("2160-0", "Creatinine [Mass/volume] in Serum or Plasma", "mg/dL",
                0.5, 1.1, critical_high=5.0, delta={"absolute": 0.3}),
        analyte("2951-2", "Sodium [Moles/volume] in Serum or Plasma", "mmol/L",
                136.0, 145.0, critical_low=120.0, critical_high=160.0),
        analyte("2823-3", "Potassium [Moles/volume] in Serum or Plasma", "mmol/L",
                3.5, 5.1, critical_low=2.8, critical_high=6.0,
                delta={"pct": 20}),
        # Diabetes monitoring (also charted by the SMART app / seed bundle)
        analyte("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "%",
                4.0, 5.6),
        # Panels (orderable; components are the resultable analytes)
        {"code": "58410-2", "display": "Complete blood count (hemogram) panel",
         "active": True, "panel": True,
         "components": ["718-7", "4544-3", "6690-2", "777-3"]},
        {"code": "51990-0", "display": "Basic metabolic panel", "active": True,
         "panel": True, "bench": "chemistry",
         "components": ["2345-7", "3094-0", "2160-0", "2951-2", "2823-3"]},
    ]
    # Phase 6B bench completions. The CBC block above predates the
    # `bench` field; stamp its bench here (haematology), then append
    # the new benches: thyroid (reflex pair), coagulation, urinalysis,
    # microbiology, blood bank, anatomic pathology.
    for concept in concepts:
        if concept["code"] in ("4544-3", "6690-2", "777-3", "58410-2"):
            concept["bench"] = "haematology"
    concepts += [
        # Thyroid — the reflex demonstration pair (low TSH -> free T4)
        analyte("3016-3", "Thyrotropin [Units/volume] in Serum or Plasma",
                "mIU/L", 0.4, 4.2),
        analyte("3024-7", "Thyroxine.free [Mass/volume] in Serum or Plasma",
                "ng/dL", 0.9, 1.7),
        # Coagulation bench
        analyte("5902-2", "Prothrombin time (PT)", "s", 9.4, 12.5,
                bench="coagulation"),
        analyte("6301-6", "INR in Blood by Coagulation assay", "",
                0.9, 1.2, bench="coagulation"),
        analyte("3173-2", "Activated partial thromboplastin time (aPTT)", "s",
                25.0, 35.0, bench="coagulation"),
        {"code": "COAG-PANEL",
         "display": "Coagulation panel (PT/INR, aPTT) — local grouping code",
         "active": True, "panel": True, "bench": "coagulation",
         "components": ["5902-2", "6301-6", "3173-2"]},
        # Urinalysis bench: dipstick components are qualitative (coded
        # values); pH + specific gravity stay numeric.
        qualitative("5792-7", "Glucose [Presence] in Urine by Test strip",
                    ["Negative", "Trace", "1+", "2+", "3+", "4+"], "urinalysis"),
        qualitative("5804-0", "Protein [Presence] in Urine by Test strip",
                    ["Negative", "Trace", "1+", "2+", "3+", "4+"], "urinalysis"),
        qualitative("5797-6", "Ketones [Presence] in Urine by Test strip",
                    ["Negative", "Trace", "Small", "Moderate", "Large"],
                    "urinalysis"),
        qualitative("5794-3", "Hemoglobin [Presence] in Urine by Test strip",
                    ["Negative", "Trace", "1+", "2+", "3+"], "urinalysis"),
        qualitative("5799-2", "Leukocyte esterase [Presence] in Urine by Test strip",
                    ["Negative", "Trace", "1+", "2+", "3+"], "urinalysis"),
        qualitative("5802-4", "Nitrite [Presence] in Urine by Test strip",
                    ["Negative", "Positive"], "urinalysis"),
        qualitative("5770-3", "Bilirubin [Presence] in Urine by Test strip",
                    ["Negative", "1+", "2+", "3+"], "urinalysis"),
        analyte("5803-2", "pH of Urine by Test strip", "pH", 5.0, 8.0,
                bench="urinalysis"),
        analyte("5811-5", "Specific gravity of Urine by Test strip", "",
                1.005, 1.030, bench="urinalysis"),
        {"code": "24356-8", "display": "Urinalysis complete panel in Urine",
         "active": True, "panel": True, "bench": "urinalysis",
         "components": ["5792-7", "5804-0", "5797-6", "5794-3", "5799-2",
                        "5802-4", "5770-3", "5803-2", "5811-5"]},
        # Microbiology bench: culture orders run the microbiology
        # workflow (app.lab_benches) — staged culture reports, never
        # numeric results on this spine.
        {"code": "87040-1", "display": "Blood culture", "active": True,
         "bench": "microbiology"},
        {"code": "87086-6", "display": "Urine culture", "active": True,
         "bench": "microbiology"},
        {"code": "87070-0", "display": "Wound culture", "active": True,
         "bench": "microbiology"},
        # Blood bank: type & screen results are qualitative on the
        # standard spine; crossmatch fulfilment is the unit
        # issue/transfusion workflow (app.lab_benches). BB-* are local
        # grouping codes (flagged in the Phase 6B SQA report).
        qualitative("883-9", "ABO group [Type] in Blood",
                    ["A POS", "A NEG", "B POS", "B NEG",
                     "AB POS", "AB NEG", "O POS", "O NEG"], "blood-bank"),
        qualitative("890-4", "Blood group antibody screen [Presence] in Serum or Plasma",
                    ["Negative", "Positive"], "blood-bank"),
        {"code": "BB-TS", "display": "Type and screen — local grouping code",
         "active": True, "panel": True, "bench": "blood-bank",
         "components": ["883-9", "890-4"]},
        qualitative("BB-XM", "Crossmatch (compatibility test) — local code",
                    ["Compatible", "Incompatible"], "blood-bank"),
        # Anatomic pathology: reported with the published synoptic form
        # through the pathology workflow (app.lab_benches).
        {"code": "11529-5", "display": "Surgical pathology study",
         "active": True, "bench": "pathology"},
    ]
    return {"name": "OpenCare laboratory catalog (LOINC)",
            "system": LOINC, "concepts": concepts,
            # QC gating policy (read by qc_blockers): the defaults below
            # reproduce the original code rule exactly — a FAILED latest
            # run blocks; missing QC history does not; no freshness
            # window. A site may publish stricter policy (block on missing
            # QC, require a passing run within N hours) without code.
            "qc": {"blockOnFailedLatest": True, "blockOnMissingQc": False,
                   "freshnessHours": None,
                   # QC programme (Phase 6B): control materials with an
                   # established mean/SD per analyte + level, and the
                   # Westgard-style rules evaluated at run creation.
                   # Flags are stored on the run; `passed` (and with it
                   # the gate) keeps its original range-based meaning.
                   "programme": {
                       "materials": [
                           {"material": "SYN-QC-LOT-1", "analyteCode": "2823-3",
                            "level": "L1", "mean": 4.0, "sd": 0.1},
                           {"material": "SYN-QC-LOT-2", "analyteCode": "2823-3",
                            "level": "L2", "mean": 5.8, "sd": 0.12},
                           {"material": "SYN-QC-LOT-1", "analyteCode": "2345-7",
                            "level": "L1", "mean": 90.0, "sd": 3.0},
                       ],
                       "rules": {"warn1_2s": True, "reject1_3s": True,
                                 "reject2_2s": True}}},
            # Specimen rejection criteria (Phase 6B): a rejection may
            # name one of these codes (validated when supplied); the
            # free-text reason remains available alongside.
            "rejectionCriteria": [
                {"code": "hemolysed", "display": "Haemolysed specimen"},
                {"code": "clotted", "display": "Clotted specimen"},
                {"code": "insufficient", "display": "Insufficient quantity"},
                {"code": "mislabelled", "display": "Mislabelled specimen"},
                {"code": "wrong-container", "display": "Wrong container"},
                {"code": "expired", "display": "Expired / too old to test"},
                {"code": "contaminated", "display": "Contaminated specimen"},
            ],
            # Collection worklist (Phase 6B): priority ordering for the
            # phlebotomy rounds list; an order without a priority uses
            # the configured default.
            "collection": {"priorityOrder": ["stat", "asap", "urgent", "routine"],
                           "defaultPriority": "routine"},
            # Reflex rules (Phase 6B): when a created result matches, a
            # draft order for `addCode` is created for the same patient,
            # flagged with the reflex extension + this rule id.
            # `when`: below-low | above-high | critical (interpretation).
            "reflexRules": [
                {"id": "tsh-low-add-ft4", "triggerCode": "3016-3",
                 "when": "below-low", "addCode": "3024-7",
                 "note": "Low TSH reflexes a free-T4 order"},
            ],
            # Auto-verification (Phase 6B): OFF by default — seeded
            # behaviour is unchanged (every draft awaits a human verify).
            # Publishing enabled=true turns it on: a draft then
            # auto-verifies only when EVERY resulted analyte carries an
            # `autoVerify` range in this catalog, the value is inside
            # it, not critical, not QC-blocked, and within its delta
            # bounds when configured. It NEVER auto-releases.
            "autoVerify": {"enabled": False},
            # Blood-bank product types (Phase 6B): unit inventory
            # validates product_type against this list; blood groups
            # validate against the ABO concept's `values` above.
            "bloodProducts": [
                {"code": "rbc", "display": "Red blood cells"},
                {"code": "plasma", "display": "Fresh frozen plasma"},
                {"code": "platelets", "display": "Platelets"},
                {"code": "cryoprecipitate", "display": "Cryoprecipitate"},
            ]}


def default_lab_workflow_payload() -> dict:
    states = sorted(DEFAULT_LAB_TRANSITIONS.keys())
    transitions = [
        {"from": src, "to": dst}
        for src in sorted(DEFAULT_LAB_TRANSITIONS)
        for dst in sorted(DEFAULT_LAB_TRANSITIONS[src])
    ]
    # governedStates / specimenStates ride in the workflow payload (they
    # used to be the GOVERNED_STATES / _SPECIMEN_MIRROR code constants;
    # the constants remain only as fallbacks for payloads without them).
    return {"states": states, "transitions": transitions,
            "governedStates": sorted(GOVERNED_STATES),
            "specimenStates": sorted(_SPECIMEN_MIRROR)}


def _workflow_payload_from(transitions_map: dict, governed: tuple,
                           specimen_states: tuple) -> dict:
    states = sorted(transitions_map.keys())
    transitions = [
        {"from": src, "to": dst}
        for src in sorted(transitions_map)
        for dst in sorted(transitions_map[src])
    ]
    return {"states": states, "transitions": transitions,
            "governedStates": sorted(governed),
            "specimenStates": sorted(specimen_states)}


def default_micro_workflow_payload() -> dict:
    """Microbiology culture machine (Phase 6B): the staged states
    (preliminary-reported -> organism-identified ->
    susceptibility-reported -> final) are governed — each is reached
    through its bench endpoint in app.lab_benches, which writes the
    staged report content, not through a bare status flip."""
    return _workflow_payload_from(
        DEFAULT_MICRO_TRANSITIONS,
        governed=("preliminary-reported", "organism-identified",
                  "susceptibility-reported", "final"),
        specimen_states=("collected", "plated", "preliminary-reported",
                         "organism-identified", "susceptibility-reported",
                         "final", "rejected"))


def default_pathology_workflow_payload() -> dict:
    """Anatomic pathology machine (Phase 6B): accession -> gross ->
    histology run through the generic transition endpoint; `reported`
    (synoptic draft written) and `signed-out` are governed — reached
    through the report / sign endpoints in app.lab_benches."""
    return _workflow_payload_from(
        DEFAULT_PATHOLOGY_TRANSITIONS,
        governed=("reported", "signed-out"),
        specimen_states=("collected", "accessioned", "grossed", "histology",
                         "reported", "signed-out", "rejected"))


def default_organisms_payload() -> dict:
    def org(code, display):
        return {"code": code, "display": display, "active": True}
    return {"name": "OpenCare microbiology organisms (local codes)",
            "concepts": [
                org("no-growth", "No growth"),
                org("staphylococcus-aureus", "Staphylococcus aureus"),
                org("escherichia-coli", "Escherichia coli"),
                org("streptococcus-pneumoniae", "Streptococcus pneumoniae"),
                org("klebsiella-pneumoniae", "Klebsiella pneumoniae"),
                org("pseudomonas-aeruginosa", "Pseudomonas aeruginosa"),
                org("candida-albicans", "Candida albicans"),
                org("mixed-flora", "Mixed flora (probable contamination)"),
            ]}


def default_antibiotics_payload() -> dict:
    def abx(code, display):
        return {"code": code, "display": display, "active": True}
    return {"name": "OpenCare microbiology antibiotics (local codes)",
            "concepts": [
                abx("oxacillin", "Oxacillin"),
                abx("vancomycin", "Vancomycin"),
                abx("ceftriaxone", "Ceftriaxone"),
                abx("ciprofloxacin", "Ciprofloxacin"),
                abx("meropenem", "Meropenem"),
                abx("gentamicin", "Gentamicin"),
                abx("fluconazole", "Fluconazole"),
            ]}


def default_synoptic_form_payload() -> dict:
    """Surgical-pathology synoptic report form (Phase 6B) — a published
    Studio FORM, completed through the Phase 5 documentation machinery
    (documentation._store_response validates required items)."""
    def item(link_id, text, type_, required=False, options=None):
        out = {"linkId": link_id, "text": text, "type": type_,
               "required": required}
        if options:
            out["answerOption"] = [
                {"valueCoding": {"code": o, "display": o}} for o in options]
        return out
    return {
        "resourceType": "Questionnaire",
        "title": "Surgical pathology synoptic report",
        "item": [
            item("specimenType", "Specimen type", "string", True),
            item("tumourSite", "Tumour site", "string", True),
            item("histologicType", "Histologic type", "choice", True,
                 ["Adenocarcinoma", "Squamous cell carcinoma",
                  "Small cell carcinoma", "Other"]),
            item("grade", "Histologic grade", "choice", False,
                 ["G1", "G2", "G3", "GX"]),
            item("marginStatus", "Margin status", "choice", True,
                 ["Negative", "Positive", "Close"]),
            item("nodesExamined", "Lymph nodes examined", "integer"),
            item("nodesPositive", "Lymph nodes positive", "integer"),
            item("comment", "Pathologist comment", "text"),
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


def ensure_lab_defaults(session: Session) -> None:
    """Seed the lab catalog + lab workflow as published Studio config v1
    (idempotent), exactly like the ADT workflow seed in studio_config.
    Phase 6B adds the bench workflows + micro terminology + the
    pathology synoptic form, seeded the same way."""
    _seed_published(session, "terminology", LAB_CATALOG_KEY,
                    default_lab_catalog_payload())
    _seed_published(session, "workflow", LAB_WORKFLOW_KEY,
                    default_lab_workflow_payload())
    _seed_published(session, "workflow", MICRO_WORKFLOW_KEY,
                    default_micro_workflow_payload())
    _seed_published(session, "workflow", PATHOLOGY_WORKFLOW_KEY,
                    default_pathology_workflow_payload())
    _seed_published(session, "terminology", ORGANISMS_KEY,
                    default_organisms_payload())
    _seed_published(session, "terminology", ANTIBIOTICS_KEY,
                    default_antibiotics_payload())
    _seed_published(session, "form", SYNOPTIC_FORM_KEY,
                    default_synoptic_form_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------

def _catalog(session: Session) -> dict:
    cfg = published_config(session, "terminology", LAB_CATALOG_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published lab catalog (terminology/{LAB_CATALOG_KEY}); "
                    "publish one in Studio before using the lab"))
    return cfg.payload or {}


def _concept(session: Session, code: str) -> dict | None:
    for concept in _catalog(session).get("concepts") or []:
        if isinstance(concept, dict) and concept.get("code") == code:
            return concept
    return None


def _is_poct(order_row: FhirResource | None) -> bool:
    """True when the order carries the POCT extension (placed with
    `poct: true` — an office point-of-care test)."""
    if order_row is None:
        return False
    for ext in (order_row.body or {}).get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == POCT_EXTENSION \
                and ext.get("valueBoolean") is True:
            return True
    return False


def _order_bench(session: Session,
                 order_row: FhirResource | None) -> str | None:
    """The bench a catalog concept assigns to this order (Phase 6B), or
    None for bench-less orders. Microbiology + pathology orders are
    routed to their own published workflow machines; all other benches
    share the standard machine."""
    if order_row is None:
        return None
    code = _code_of(order_row.body or {})
    if not code:
        return None
    try:
        concept = _concept(session, code)
    except HTTPException:
        return None
    return concept.get("bench") if concept else None


_BENCH_FALLBACKS = {"microbiology": DEFAULT_MICRO_TRANSITIONS,
                    "pathology": DEFAULT_PATHOLOGY_TRANSITIONS}


def _workflow_payload_for(session: Session,
                          order_row: FhirResource | None) -> dict | None:
    """The workflow payload that governs this order: the POCT variant
    for a poct-flagged order when that variant is published, the bench
    workflow for microbiology/pathology orders, else the standard lab
    workflow payload."""
    if _is_poct(order_row):
        cfg = published_config(session, "workflow", POCT_WORKFLOW_KEY)
        if cfg is not None:
            return cfg.payload or {}
    bench_key = BENCH_WORKFLOW_KEYS.get(_order_bench(session, order_row) or "")
    if bench_key:
        return workflow_payload(session, bench_key)
    return workflow_payload(session, LAB_WORKFLOW_KEY)


def _lab_machine(session: Session, order_row: FhirResource | None = None):
    """(transitions, known states, per-transition roles) from the
    published lab workflow config governing this order (the POCT
    variant for poct-flagged orders when published; the bench machine
    for microbiology/pathology orders); falls back to the built-in
    map when no config is published."""
    if _is_poct(order_row):
        cfg = published_config(session, "workflow", POCT_WORKFLOW_KEY)
        if cfg is not None:
            return machine_from_payload(cfg.payload, DEFAULT_LAB_TRANSITIONS)
    bench = _order_bench(session, order_row)
    bench_key = BENCH_WORKFLOW_KEYS.get(bench or "")
    if bench_key:
        return load_workflow_machine(session, bench_key,
                                     _BENCH_FALLBACKS[bench])
    return load_workflow_machine(session, LAB_WORKFLOW_KEY,
                                 DEFAULT_LAB_TRANSITIONS)


def _governed_states(session: Session,
                     order_row: FhirResource | None = None) -> set:
    """States the generic transition endpoint will not move INTO, from
    the governing workflow payload's `governedStates` (fallback: the
    built-in set)."""
    return payload_state_set(_workflow_payload_for(session, order_row),
                             "governedStates", GOVERNED_STATES)


def _specimen_states(session: Session,
                     order_row: FhirResource | None = None) -> set:
    """States mirrored onto the linked Specimen, from the governing
    workflow payload's `specimenStates` (fallback: the built-in mirror
    set)."""
    return payload_state_set(_workflow_payload_for(session, order_row),
                             "specimenStates", _SPECIMEN_MIRROR)


# ---------------------------------------------------------------------------
# Resource helpers (staff-side: lab routes are role-gated, so internal row
# access here does not apply patient scoping — patients never reach /lab)
# ---------------------------------------------------------------------------

def _order_row(session: Session, order_id: str) -> FhirResource:
    return fhir._get_row("ServiceRequest", order_id, session)


def _specimen_row(session: Session, order_row: FhirResource) -> FhirResource | None:
    for ref in (order_row.body or {}).get("specimen") or []:
        reference = ref.get("reference") if isinstance(ref, dict) else None
        if reference and reference.startswith("Specimen/"):
            return fhir._get_row("Specimen", reference.split("/", 1)[1], session)
    return None


def _report_order(session: Session, report_row: FhirResource) -> FhirResource | None:
    for ref in (report_row.body or {}).get("basedOn") or []:
        reference = ref.get("reference") if isinstance(ref, dict) else None
        if reference and reference.startswith("ServiceRequest/"):
            try:
                return fhir._get_row("ServiceRequest", reference.split("/", 1)[1], session)
            except HTTPException:
                return None
    return None


def _result_observations(session: Session, report_row: FhirResource) -> list[FhirResource]:
    rows = []
    for ref in (report_row.body or {}).get("result") or []:
        reference = ref.get("reference") if isinstance(ref, dict) else None
        if reference and reference.startswith("Observation/"):
            rows.append(fhir._get_row("Observation", reference.split("/", 1)[1], session))
    return rows


def _set_status(row: FhirResource, status: str) -> None:
    body = dict(row.body or {})
    body["status"] = status
    row.body = body
    row.status = status
    row.version += 1


def _code_of(concept_body: dict) -> str | None:
    code = (concept_body or {}).get("code") or {}
    coding = code.get("coding") or []
    if coding and isinstance(coding[0], dict):
        return coding[0].get("code")
    return code.get("text") if isinstance(code, dict) else None


def _order_code(session: Session, order_row: FhirResource) -> dict:
    code = _code_of(order_row.body or {})
    concept = _concept(session, code) if code else None
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Order {order_row.resource_id} names code '{code}', which is "
                   "not in the published lab catalog")
    return concept


# ---------------------------------------------------------------------------
# Interpretation (reference ranges + critical thresholds from config)
# ---------------------------------------------------------------------------

_INTERPRETATION_TEXT = {
    "N": "Normal", "L": "Low", "H": "High",
    "LL": "Critical low", "HH": "Critical high",
}


def interpret_value(value: float, concept: dict) -> str | None:
    """HL7 v3 ObservationInterpretation code for a numeric result against
    the analyte's configured range. Critical thresholds win over the
    reference range; returns None when the analyte has no range data."""
    rng = concept.get("referenceRange") or {}
    low, high = rng.get("low"), rng.get("high")
    crit_low, crit_high = rng.get("criticalLow"), rng.get("criticalHigh")
    if crit_low is None and crit_high is None and low is None and high is None:
        return None
    if crit_low is not None and value <= crit_low:
        return "LL"
    if crit_high is not None and value >= crit_high:
        return "HH"
    if low is not None and value < low:
        return "L"
    if high is not None and value > high:
        return "H"
    return "N"


def _is_critical(code: str | None) -> bool:
    return code in ("LL", "HH")


# ---------------------------------------------------------------------------
# QC gating
# ---------------------------------------------------------------------------

def _qc_out(run: LabQcRun) -> dict:
    return {
        "id": run.id, "analyte_code": run.analyte_code,
        "control_material": run.control_material,
        "expected_low": run.expected_low, "expected_high": run.expected_high,
        "measured_value": run.measured_value, "unit": run.unit,
        "passed": run.passed, "performed_by": run.performed_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        # QC programme (Phase 6B): control level + Westgard-style flags
        # + z-score, computed at creation from the programme config.
        "level": run.level, "flags": list(run.flags or []),
        "z_score": run.z_score,
    }


def _qc_programme(session: Session) -> dict:
    """The QC programme config from the published catalog's
    `qc.programme` (materials with established mean/SD + the
    Westgard-style rule switches). Empty when unpublished."""
    cfg = published_config(session, "terminology", LAB_CATALOG_KEY)
    programme = ((cfg.payload or {}).get("qc") or {}).get("programme") \
        if cfg is not None else None
    return programme if isinstance(programme, dict) else {}


def _westgard_flags(session: Session, analyte_code: str, material: str,
                    level: str | None, measured: float) -> tuple[list[str], float | None, str | None]:
    """Westgard-style rule evaluation (Phase 6B) for one new QC run.

    Looks the (material, analyte) up in the published programme config
    for its established mean/SD; computes z = (measured - mean) / sd
    and flags: |z| > 2 -> "1-2s" (warning), |z| > 3 -> "1-3s"
    (rejection), and a previous run of the same analyte + material +
    level also beyond 2 SD on the SAME side -> "2-2s" (rejection).
    Rules are switches in the same config. Returns (flags, z, level) —
    the level falls back to the material's configured level. No
    programme entry for the material: no flags, no z (the run still
    records, and `passed` still comes from the expected range)."""
    programme = _qc_programme(session)
    entry = None
    for candidate in programme.get("materials") or []:
        if isinstance(candidate, dict) \
                and candidate.get("material") == material \
                and candidate.get("analyteCode") == analyte_code:
            entry = candidate
            break
    if entry is None:
        return [], None, level
    mean, sd = entry.get("mean"), entry.get("sd")
    if not isinstance(mean, (int, float)) or not isinstance(sd, (int, float)) \
            or sd <= 0:
        return [], None, level or entry.get("level")
    z = (measured - float(mean)) / float(sd)
    rules = programme.get("rules") or {}
    flags: list[str] = []
    if rules.get("warn1_2s", True) and abs(z) > 2:
        flags.append("1-2s")
    if rules.get("reject1_3s", True) and abs(z) > 3:
        flags.append("1-3s")
    effective_level = level or entry.get("level")
    if rules.get("reject2_2s", True) and abs(z) > 2:
        previous = session.scalars(
            select(LabQcRun).where(LabQcRun.analyte_code == analyte_code,
                                   LabQcRun.control_material == material)
            .order_by(LabQcRun.id.desc())).first()
        if previous is not None and previous.z_score is not None \
                and (previous.level or entry.get("level")) == effective_level \
                and abs(previous.z_score) > 2 \
                and (previous.z_score > 0) == (z > 0):
            flags.append("2-2s")
    return flags, z, effective_level


def _latest_qc(session: Session, analyte_code: str) -> LabQcRun | None:
    return session.scalars(
        select(LabQcRun).where(LabQcRun.analyte_code == analyte_code)
        .order_by(LabQcRun.id.desc())).first()


def _qc_policy(session: Session) -> dict:
    """QC gating policy from the published lab catalog's `qc` object.
    The defaults reproduce the original code rule exactly: a failed
    LATEST run blocks; missing QC history does not block; there is no
    freshness window. A site publishes stricter policy as data —
    `blockOnMissingQc` (no run at all blocks) and/or `freshnessHours`
    (the latest passing run must be younger than this) — or relaxes the
    failed-latest block with `blockOnFailedLatest: false`."""
    policy = {"blockOnFailedLatest": True, "blockOnMissingQc": False,
              "freshnessHours": None}
    cfg = published_config(session, "terminology", LAB_CATALOG_KEY)
    raw = (cfg.payload or {}).get("qc") if cfg is not None else None
    if isinstance(raw, dict):
        for field in ("blockOnFailedLatest", "blockOnMissingQc"):
            if isinstance(raw.get(field), bool):
                policy[field] = raw[field]
        fresh = raw.get("freshnessHours")
        if isinstance(fresh, (int, float)) and not isinstance(fresh, bool) \
                and fresh > 0:
            policy["freshnessHours"] = float(fresh)
    return policy


def qc_blockers(session: Session, analyte_codes) -> list[dict]:
    """Analytes currently blocked by the QC policy (see _qc_policy).
    Each blocker carries the analyte, the deciding run (None when the
    block is missing QC history) and a machine-readable `reason`:
    'failed-latest' | 'missing-qc' | 'stale-qc'."""
    policy = _qc_policy(session)
    now = datetime.now(timezone.utc)
    blockers = []
    for code in sorted(set(analyte_codes)):
        latest = _latest_qc(session, code)
        if latest is None:
            if policy["blockOnMissingQc"]:
                blockers.append({"analyte_code": code, "qc_run": None,
                                 "reason": "missing-qc"})
            continue
        if not latest.passed:
            if policy["blockOnFailedLatest"]:
                blockers.append({"analyte_code": code,
                                 "qc_run": _qc_out(latest),
                                 "reason": "failed-latest"})
            continue
        freshness = policy["freshnessHours"]
        if freshness is not None and latest.created_at is not None:
            created = latest.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (now - created).total_seconds() > freshness * 3600:
                blockers.append({"analyte_code": code,
                                 "qc_run": _qc_out(latest),
                                 "reason": "stale-qc"})
    return blockers


def _qc_block_message(blockers: list[dict]) -> str:
    names = ", ".join(b["analyte_code"] for b in blockers)
    reasons = ", ".join(sorted({b["reason"] for b in blockers}))
    return (f"QC block: {names} [{reasons}]; "
            "record a passing QC run for each blocked analyte "
            "before this report can proceed")


def _enforce_qc(session: Session, report_row: FhirResource) -> None:
    codes = [_code_of(o.body or {}) for o in _result_observations(session, report_row)]
    blockers = qc_blockers(session, [c for c in codes if c])
    if blockers:
        raise HTTPException(status_code=409, detail=_qc_block_message(blockers))


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


def _extract_order_code(body: dict) -> str | None:
    raw = body.get("code")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return _code_of(body)
    return None


@router.post("/orders", status_code=201)
async def place_order(request: Request, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.order"))):
    """Place a lab order. The code must be an ACTIVE concept (panel or
    analyte) in the published Studio lab catalog — ordering is configuration
    validation, not a free-text request."""
    body = await _json_body(request)
    code = _extract_order_code(body)
    if not code:
        raise HTTPException(status_code=422,
                            detail="Lab order requires 'code' (a catalog code or "
                                   "a CodeableConcept naming one)")
    concept = _concept(session, code)
    if concept is None:
        raise HTTPException(status_code=422,
                            detail=f"Unknown lab code '{code}': not in the "
                                   "published lab catalog")
    if concept.get("active") is False:
        raise HTTPException(status_code=422,
                            detail=f"Lab code '{code}' ({concept.get('display')}) "
                                   "is inactive in the published lab catalog")
    subject = body.get("subject") or {}
    subject_ref = subject.get("reference") if isinstance(subject, dict) else None
    if not subject_ref and body.get("patient"):
        subject_ref = fhir._normalize_patient_ref(str(body["patient"]))
    if not subject_ref:
        raise HTTPException(status_code=422,
                            detail="Lab order requires 'subject.reference' "
                                   "(or 'patient')")
    if not subject_ref.startswith("Patient/"):
        raise HTTPException(status_code=422,
                            detail="Lab order subject must reference a Patient")
    patient_id = subject_ref.split("/", 1)[1]
    fhir._get_row("Patient", patient_id, session)  # 404 if the patient is unknown

    order_body = {
        "resourceType": "ServiceRequest",
        "status": "ordered",
        "intent": "order",
        "category": [{"coding": [{
            "system": "http://snomed.info/sct", "code": "108252007",
            "display": "Laboratory procedure"}]}],
        "code": {"coding": [{"system": LOINC, "code": code,
                             "display": concept.get("display")}],
                 "text": concept.get("display")},
        "subject": {"reference": subject_ref},
        "authoredOn": _now_iso(),
        "requester": {"display": user.username},
    }
    if body.get("id"):
        order_body["id"] = str(body["id"])
    if isinstance(body.get("encounter"), dict) and body["encounter"].get("reference"):
        order_body["encounter"] = {"reference": body["encounter"]["reference"]}
    if body.get("priority"):
        order_body["priority"] = body["priority"]
    if body.get("poct") is True:
        # Office point-of-care test: the order runs on the alternate
        # POCT workflow (single-step result release) when published.
        order_body["extension"] = [{"url": POCT_EXTENSION, "valueBoolean": True}]
    return fhir.create_resource("ServiceRequest", order_body, session, user)


@router.get("/orders")
def list_orders(status: str | None = Query(default=None),
                patient: str | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("lab.read"))):
    stmt = select(FhirResource).where(FhirResource.resource_type == "ServiceRequest")
    if status:
        stmt = stmt.where(FhirResource.status == status)
    if patient:
        stmt = stmt.where(FhirResource.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.order_by(FhirResource.created_at.asc(),
                                         FhirResource.id.asc())).all()
    return fhir._bundle(rows)


@router.get("/orders/{order_id}")
def order_detail(order_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.read"))):
    order = _order_row(session, order_id)
    specimen = _specimen_row(session, order)
    reports = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "DiagnosticReport")
        .order_by(FhirResource.created_at.asc())).all()
    own_reports = []
    for report in reports:
        refs = [(ref or {}).get("reference")
                for ref in (report.body or {}).get("basedOn") or []]
        if f"ServiceRequest/{order_id}" in refs:
            own_reports.append(fhir._render(report))
    return {"order": fhir._render(order),
            "specimen": fhir._render(specimen) if specimen else None,
            "reports": own_reports,
            # Chain of custody (Phase 6B): ordered -> collected ->
            # received -> in-analysis timestamps, null where a step
            # has not happened. The rejection history (when any) rides
            # on the order body itself as `rejectionHistory`.
            "custody": _custody(order, specimen)}


@router.post("/orders/{order_id}/transition")
async def transition_order(order_id: str, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    """Advance an order (and its specimen, mirrored) through the workflow
    config. Resulted/verified/released are governed states with dedicated
    endpoints — a bare status flip into them is refused with 422."""
    payload = await _json_body(request)
    target = payload.get("to_status") or payload.get("status")
    if not target:
        raise HTTPException(status_code=422, detail="Transition requires 'to_status'")
    order = _order_row(session, order_id)
    if target in _governed_states(session, order):
        raise HTTPException(
            status_code=422,
            detail=(f"Status '{target}' is governed: it is reached through the "
                    "result-entry, verify and release endpoints, not a bare "
                    "transition"))
    current = (order.body or {}).get("status")
    transitions, known_states, roles = _lab_machine(session, order)
    if target not in known_states:
        raise HTTPException(status_code=422, detail=f"Unknown lab order status: {target}")
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=409,
            detail=f"Illegal lab order transition '{current}' -> '{target}'")
    if not transition_role_allowed(roles, current, target, user.role.value):
        raise HTTPException(
            status_code=403,
            detail=(f"Role {user.role.value} may not perform the lab order "
                    f"transition '{current}' -> '{target}': the published "
                    "workflow config restricts it to "
                    f"{', '.join(roles.get((current, target), []))}"))

    specimen = _specimen_row(session, order)
    specimen_id = None
    specimen_fresh = False
    if target == "collected":
        if specimen is not None:
            raise HTTPException(status_code=409,
                                detail="Order already has a collected specimen")
        specimen_type = payload.get("specimen_type") or "Blood specimen"
        count = len(session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Specimen")).all())
        accession = (f"ACC-{datetime.now(timezone.utc):%Y%m%d}-{count + 1:04d}")
        specimen_body = {
            "resourceType": "Specimen",
            "id": str(uuid.uuid4()),
            "status": "collected",
            "accessionIdentifier": {"system": "urn:opencare:accession",
                                    "value": accession},
            "type": {"text": specimen_type},
            "subject": {"reference": (order.body or {}).get("subject", {}).get("reference")},
            "request": [{"reference": f"ServiceRequest/{order_id}"}],
            "collection": {"collectedDateTime": _now_iso()},
        }
        rendered = fhir.create_resource("Specimen", specimen_body, session, user)
        specimen_id = rendered["id"]
        specimen_fresh = True  # created already carrying the target status
        order_body = dict(order.body or {})
        order_body["specimen"] = [{"reference": f"Specimen/{specimen_id}"}]
        order.body = order_body
        specimen = fhir._get_row("Specimen", specimen_id, session)
    elif target == "received":
        if specimen is None:
            raise HTTPException(status_code=409,
                                detail="Order has no specimen to receive")
        body = dict(specimen.body or {})
        body["receivedDateTime"] = _now_iso()
        specimen.body = body
    elif target == "in-process":
        # Chain of custody (Phase 6B): stamp when analysis began, on
        # the specimen (collected + received stamps already live there:
        # collection.collectedDateTime and receivedDateTime).
        if specimen is not None:
            body = dict(specimen.body or {})
            exts = [e for e in (body.get("extension") or [])
                    if not (isinstance(e, dict)
                            and e.get("url") == IN_ANALYSIS_EXTENSION)]
            exts.append({"url": IN_ANALYSIS_EXTENSION,
                         "valueDateTime": _now_iso()})
            body["extension"] = exts
            specimen.body = body
    elif target == "rejected":
        reason = payload.get("reason")
        if not reason or not isinstance(reason, str):
            raise HTTPException(status_code=422,
                                detail="Rejecting a specimen requires 'reason'")
        # Coded criterion (Phase 6B): when supplied, it must be one of
        # the published catalog's rejectionCriteria.
        criterion = None
        reason_code = payload.get("reason_code")
        if reason_code is not None:
            criteria = {c.get("code"): c
                        for c in _catalog(session).get("rejectionCriteria") or []
                        if isinstance(c, dict)}
            criterion = criteria.get(reason_code)
            if criterion is None:
                raise HTTPException(
                    status_code=422,
                    detail=(f"Unknown rejection criterion '{reason_code}': "
                            "not in the published lab catalog's "
                            "rejectionCriteria"))
        if specimen is not None:
            body = dict(specimen.body or {})
            label = (f"{criterion['display']} — {reason}" if criterion
                     else reason)
            body["note"] = [{"text": f"Rejected: {label}"}]
            specimen.body = body
        order_body = dict(order.body or {})
        status_reason = {"text": reason}
        if criterion is not None:
            status_reason["coding"] = [{
                "system": "urn:opencare:rejection-criteria",
                "code": criterion["code"], "display": criterion["display"]}]
        order_body["statusReason"] = status_reason
        # Rejection history + recollect task (Phase 6B): the rejected
        # specimen is NEVER deleted; the order records what happened
        # and a Task asks phlebotomy to recollect. The recollect
        # endpoint returns the order to `ordered` and completes it.
        history = list(order_body.get("rejectionHistory") or [])
        history.append({
            "criterion_code": criterion["code"] if criterion else None,
            "criterion": criterion["display"] if criterion else None,
            "reason": reason, "at": _now_iso(), "by": user.username,
            "specimen_id": specimen.resource_id if specimen else None,
            "accession": ((specimen.body or {}).get("accessionIdentifier")
                          or {}).get("value") if specimen else None,
        })
        order_body["rejectionHistory"] = history
        order.body = order_body
        subject_ref = (order.body or {}).get("subject", {}).get("reference")
        fhir.create_resource("Task", {
            "resourceType": "Task", "id": str(uuid.uuid4()),
            "status": "requested", "intent": "order",
            "code": {"coding": [{"system": "urn:opencare:task-type",
                                 "code": "recollect",
                                 "display": "Recollect specimen"}],
                     "text": "Recollect specimen"},
            "focus": {"reference": f"ServiceRequest/{order_id}"},
            "for": {"reference": subject_ref},
            "authoredOn": _now_iso(),
            "requester": {"display": user.username},
            "description": (f"Recollect specimen for rejected order "
                            f"{order_id}: {reason}"),
        }, session, user)

    _set_status(order, target)
    if specimen is not None and target in _specimen_states(session, order) \
            and not specimen_fresh:
        _set_status(specimen, target)
        specimen_id = specimen.resource_id
    session.flush()
    append_audit(session, actor=user.username, action="lab.order.transition",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"from_status": current, "to_status": target,
                          "specimen_id": specimen_id,
                          "reason": payload.get("reason")})
    session.commit()
    return {"order": fhir._render(order),
            "specimen": fhir._render(specimen) if specimen else None}


# ---------------------------------------------------------------------------
# Pre-analytical (Phase 6B): collection worklist + recollection
# ---------------------------------------------------------------------------

def _custody(order: FhirResource, specimen: FhirResource | None) -> dict:
    """Chain-of-custody timestamps for one order: ordered (the order's
    authoredOn) -> collected -> received -> in-analysis (stamps on the
    specimen). Missing steps are null, never invented."""
    custody = {"ordered_at": (order.body or {}).get("authoredOn"),
               "collected_at": None, "received_at": None,
               "in_analysis_at": None}
    if specimen is not None:
        body = specimen.body or {}
        custody["collected_at"] = (body.get("collection") or {}).get(
            "collectedDateTime")
        custody["received_at"] = body.get("receivedDateTime")
        for ext in body.get("extension") or []:
            if isinstance(ext, dict) and ext.get("url") == IN_ANALYSIS_EXTENSION:
                custody["in_analysis_at"] = ext.get("valueDateTime")
    return custody


@router.get("/collection-worklist")
def collection_worklist(session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.read"))):
    """Phlebotomy rounds list (Phase 6B): orders still in `ordered`
    state (nothing collected yet), grouped by patient + location (the
    order's encounter reference when present), each group carrying its
    highest-priority order first. Priority ordering + the default
    priority come from the published catalog's `collection` config."""
    catalog = _catalog(session)
    collection = catalog.get("collection") or {}
    priority_order = collection.get("priorityOrder") or [
        "stat", "asap", "urgent", "routine"]
    default_priority = collection.get("defaultPriority") or "routine"
    rank = {name: i for i, name in enumerate(priority_order)}

    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "ServiceRequest",
                                   FhirResource.status == "ordered")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        body = row.body or {}
        # Only lab orders: they carry the laboratory category coding.
        categories = body.get("category") or []
        is_lab = any(
            isinstance(c, dict) and any(
                isinstance(cd, dict) and cd.get("code") == "108252007"
                for cd in c.get("coding") or [])
            for c in categories)
        if not is_lab:
            continue
        patient_ref = (body.get("subject") or {}).get("reference") or ""
        location = (body.get("encounter") or {}).get("reference") or "unspecified"
        priority = body.get("priority") or default_priority
        key = (patient_ref, location)
        group = groups.setdefault(key, {"patient_ref": patient_ref,
                                        "location": location, "orders": []})
        group["orders"].append({
            "order_id": row.resource_id,
            "code": _code_of(body),
            "display": (body.get("code") or {}).get("text"),
            "priority": priority,
            "authored_on": body.get("authoredOn"),
            "custody": _custody(row, None),
        })
    out_groups = []
    for group in groups.values():
        group["orders"].sort(
            key=lambda o: (rank.get(o["priority"], len(rank)),
                           o["authored_on"] or ""))
        group["highest_priority"] = group["orders"][0]["priority"]
        out_groups.append(group)
    out_groups.sort(key=lambda g: (rank.get(g["highest_priority"], len(rank)),
                                   g["patient_ref"]))
    return {"groups": out_groups,
            "total_orders": sum(len(g["orders"]) for g in out_groups)}


def _recollect_tasks(session: Session, order_id: str) -> list[FhirResource]:
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Task")
        .order_by(FhirResource.created_at.asc())).all()
    out = []
    for row in rows:
        body = row.body or {}
        if (body.get("focus") or {}).get("reference") != f"ServiceRequest/{order_id}":
            continue
        codings = (body.get("code") or {}).get("coding") or []
        if any(isinstance(c, dict) and c.get("code") == "recollect"
               for c in codings):
            out.append(row)
    return out


@router.post("/orders/{order_id}/recollect")
def recollect_order(order_id: str, session: Session = Depends(get_db),
                    user: User = Depends(require_capability("lab.work"))):
    """Recollection (Phase 6B): return a REJECTED order to `ordered` so
    a fresh specimen can be collected. The rejected specimen is kept,
    untouched, in its rejected state (it is the audit trail); the
    order's specimen link is cleared so the next collection creates a
    new accession, the rejection history stays on the order, and the
    outstanding recollect Task(s) are completed."""
    order = _order_row(session, order_id)
    if (order.body or {}).get("status") != "rejected":
        raise HTTPException(
            status_code=409,
            detail=(f"Order {order_id} is not rejected (status "
                    f"'{(order.body or {}).get('status')}'); only a rejected "
                    "order can be recollected"))
    specimen = _specimen_row(session, order)
    body = dict(order.body or {})
    body.pop("specimen", None)
    body.pop("statusReason", None)
    order.body = body
    _set_status(order, "ordered")
    completed = []
    for task in _recollect_tasks(session, order_id):
        if (task.body or {}).get("status") == "requested":
            t = dict(task.body or {})
            t["status"] = "completed"
            t["lastModified"] = _now_iso()
            task.body = t
            task.status = "completed"
            task.version += 1
            completed.append(task.resource_id)
    session.flush()
    append_audit(session, actor=user.username, action="lab.order.recollect",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"rejected_specimen_id":
                          specimen.resource_id if specimen else None,
                          "tasks_completed": completed,
                          "rejection_count":
                          len(body.get("rejectionHistory") or [])})
    session.commit()
    return {"order": fhir._render(order), "specimen": None,
            "rejected_specimen_id": specimen.resource_id if specimen else None,
            "tasks_completed": completed}


# ---------------------------------------------------------------------------
# Specimens
# ---------------------------------------------------------------------------

@router.get("/specimens")
def list_specimens(status: str | None = Query(default=None),
                   patient: str | None = Query(default=None),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    stmt = select(FhirResource).where(FhirResource.resource_type == "Specimen")
    if status:
        stmt = stmt.where(FhirResource.status == status)
    if patient:
        stmt = stmt.where(FhirResource.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.order_by(FhirResource.created_at.asc(),
                                         FhirResource.id.asc())).all()
    return fhir._bundle(rows)


# ---------------------------------------------------------------------------
# Results: creation core shared by manual entry and analyser ingest
# ---------------------------------------------------------------------------

def _validate_results(session: Session, order: FhirResource,
                      results) -> list[dict]:
    """Validate raw [{code, value, unit?}] against the order + catalog.
    Returns [{concept, value, unit, qualitative}]. Panel orders accept
    only their component analytes; single-analyte orders accept only
    their own code.

    Phase 6B: a concept flagged `qualitative` in the catalog takes a
    coded STRING value (validated against the concept's `values` list
    when one is published) and never a number; every other concept
    keeps the original numeric-only rule, byte for byte."""
    if not isinstance(results, list) or not results:
        raise HTTPException(status_code=422,
                            detail="'results' must be a non-empty list")
    concept = _order_code(session, order)
    if concept.get("panel"):
        allowed = set(concept.get("components") or [])
        order_label = f"panel {concept['code']} components"
    else:
        allowed = {concept["code"]}
        order_label = f"ordered analyte {concept['code']}"
    checked = []
    for i, item in enumerate(results):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f"results[{i}] must be an object")
        code = item.get("code")
        analyte = _concept(session, code) if code else None
        if analyte is None:
            raise HTTPException(status_code=422,
                                detail=f"results[{i}].code '{code}' is not in the "
                                       "published lab catalog")
        if analyte.get("panel"):
            raise HTTPException(status_code=422,
                                detail=f"results[{i}].code '{code}' is a panel; "
                                       "results are entered per analyte")
        if analyte.get("active") is False:
            raise HTTPException(status_code=422,
                                detail=f"results[{i}].code '{code}' is inactive in "
                                       "the published lab catalog")
        if code not in allowed:
            raise HTTPException(status_code=422,
                                detail=f"results[{i}].code '{code}' is not one of "
                                       f"the {order_label} for this order")
        value = item.get("value")
        if analyte.get("qualitative"):
            if not isinstance(value, str) or not value.strip():
                raise HTTPException(
                    status_code=422,
                    detail=(f"results[{i}].value for qualitative analyte "
                            f"'{code}' must be a coded string (one of the "
                            "catalog concept's values)"))
            allowed_values = analyte.get("values") or []
            if allowed_values and value not in allowed_values:
                raise HTTPException(
                    status_code=422,
                    detail=(f"results[{i}].value '{value}' is not an allowed "
                            f"value for '{code}' (allowed: "
                            f"{', '.join(allowed_values)})"))
            checked.append({"concept": analyte, "value": value.strip(),
                            "unit": None, "qualitative": True})
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422,
                                detail=f"results[{i}].value must be a number")
        unit = item.get("unit") or analyte.get("unit") or \
            (analyte.get("referenceRange") or {}).get("unit")
        checked.append({"concept": analyte, "value": float(value),
                        "unit": unit, "qualitative": False})
    return checked


# ---------------------------------------------------------------------------
# Post-analytical (Phase 6B): delta checks, reflex rules, auto-verification
# ---------------------------------------------------------------------------

def _previous_released_value(session: Session, patient_ref: str | None,
                             code: str) -> float | None:
    """The patient's most recent RELEASED (final, untagged) numeric
    value for an analyte — the delta-check baseline. Drafts never
    count: they are preliminary + visibility-tagged by construction."""
    if not patient_ref:
        return None
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Observation",
                                   FhirResource.patient_ref == patient_ref,
                                   FhirResource.status == "final")
        .order_by(FhirResource.id.desc())).all()
    for row in rows:
        if fhir.has_unreleased_tag(row.body):
            continue
        if _code_of(row.body or {}) != code:
            continue
        value = (row.body or {}).get("valueQuantity", {}).get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _delta_flag(session: Session, patient_ref: str | None,
                concept: dict, current_value: float) -> dict | None:
    """A delta-check flag for one numeric result, or None. Thresholds
    are the analyte's catalog `delta` config ({absolute} or {pct});
    analytes without delta config never flag. A flag is information
    for the verifying clinician — it never blocks anything."""
    delta_cfg = concept.get("delta")
    if not isinstance(delta_cfg, dict):
        return None
    previous = _previous_released_value(session, patient_ref, concept["code"])
    if previous is None:
        return None
    change = current_value - previous
    change_pct = (abs(change) / abs(previous) * 100.0) if previous else None
    exceeded = False
    if isinstance(delta_cfg.get("absolute"), (int, float)) \
            and abs(change) > delta_cfg["absolute"]:
        exceeded = True
    if isinstance(delta_cfg.get("pct"), (int, float)) and change_pct is not None \
            and change_pct > delta_cfg["pct"]:
        exceeded = True
    if not exceeded:
        return None
    return {"code": concept["code"], "previous_value": previous,
            "current_value": current_value, "change": change,
            "change_pct": change_pct}


def _reflex_order_exists(session: Session, patient_ref: str | None,
                         report_id: str, rule_id: str | None) -> bool:
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "ServiceRequest",
                                   FhirResource.patient_ref == patient_ref)).all()
    for row in rows:
        for ext in (row.body or {}).get("extension") or []:
            if not isinstance(ext, dict):
                continue
            if ext.get("url") == REFLEX_EXTENSION and ext.get("valueString") == rule_id:
                return True
    return False


def _fire_reflex_rules(session: Session, user: User, order: FhirResource,
                       report_id: str, checked: list[dict]) -> list[str]:
    """Reflex testing (Phase 6B): for each created result matching a
    published reflex rule, create a DRAFT (status `ordered`) order for
    the rule's add-on analyte, flagged with the reflex extension + the
    rule id and linked to the source report. Deduped per (report,
    rule): re-entry never double-creates."""
    rules = _catalog(session).get("reflexRules") or []
    if not rules:
        return []
    subject_ref = (order.body or {}).get("subject", {}).get("reference")
    created: list[str] = []
    fired_rules: set[str] = set()
    for entry in checked:
        if entry["qualitative"]:
            continue
        analyte, value = entry["concept"], entry["value"]
        rng = analyte.get("referenceRange") or {}
        interp = interpret_value(value, analyte)
        for rule in rules:
            if not isinstance(rule, dict) \
                    or rule.get("triggerCode") != analyte["code"] \
                    or rule.get("id") in fired_rules:
                continue
            when = rule.get("when")
            fired = (
                (when == "below-low" and rng.get("low") is not None
                 and value < rng["low"])
                or (when == "above-high" and rng.get("high") is not None
                    and value > rng["high"])
                or (when == "critical" and _is_critical(interp)))
            if not fired:
                continue
            add_code = rule.get("addCode")
            target = _concept(session, add_code) if add_code else None
            if target is None or target.get("active") is False \
                    or target.get("panel"):
                continue
            if _reflex_order_exists(session, subject_ref, report_id,
                                    rule.get("id")):
                fired_rules.add(rule.get("id"))
                continue
            rendered = fhir.create_resource("ServiceRequest", {
                "resourceType": "ServiceRequest",
                "id": str(uuid.uuid4()),
                "status": "ordered", "intent": "order",
                "reflex": True,
                "category": [{"coding": [{
                    "system": "http://snomed.info/sct", "code": "108252007",
                    "display": "Laboratory procedure"}]}],
                "code": {"coding": [{"system": LOINC, "code": add_code,
                                     "display": target.get("display")}],
                         "text": target.get("display")},
                "subject": {"reference": subject_ref},
                "authoredOn": _now_iso(),
                "requester": {"display": f"system:reflex ({rule.get('id')})"},
                "extension": [
                    {"url": REFLEX_EXTENSION, "valueString": rule.get("id")},
                    {"url": REFLEX_SOURCE_EXTENSION,
                     "valueReference": {"reference": f"DiagnosticReport/{report_id}"}},
                ],
            }, session, user)
            append_audit(session, actor="system:reflex",
                         action="lab.reflex.create",
                         entity=f"ServiceRequest/{rendered['id']}",
                         payload={"rule_id": rule.get("id"),
                                  "trigger_code": analyte["code"],
                                  "trigger_value": value,
                                  "source_report_id": report_id,
                                  "source_order_id": order.resource_id})
            session.commit()
            fired_rules.add(rule.get("id"))
            created.append(rendered["id"])
    return created


def _auto_verify_decision(session: Session, order: FhirResource,
                          checked: list[dict]) -> tuple[bool, list[str]]:
    """Auto-verification rules (Phase 6B), all from the published
    catalog: the global switch must be on; EVERY resulted analyte must
    carry an `autoVerify` range; the value must be inside it, not
    critical, and within its delta bounds when the analyte configures
    a delta. Returns (applies, rule ids)."""
    settings = _catalog(session).get("autoVerify") or {}
    if settings.get("enabled") is not True:
        return False, []
    subject_ref = (order.body or {}).get("subject", {}).get("reference")
    rule_ids: list[str] = []
    for entry in checked:
        if entry["qualitative"]:
            return False, []
        rule = entry["concept"].get("autoVerify")
        if not isinstance(rule, dict):
            return False, []
        value = entry["value"]
        if isinstance(rule.get("low"), (int, float)) and value < rule["low"]:
            return False, []
        if isinstance(rule.get("high"), (int, float)) and value > rule["high"]:
            return False, []
        if _is_critical(interpret_value(value, entry["concept"])):
            return False, []
        if _delta_flag(session, subject_ref, entry["concept"], value) is not None:
            return False, []
        rule_ids.append(f"{entry['concept']['code']}:range")
    return True, rule_ids


def _auto_verify(session: Session, report: FhirResource, order: FhirResource,
                 checked: list[dict], rule_ids: list[str]) -> bool:
    """Perform the auto-verification mutation (report -> final, order
    resulted -> verified; the report stays UNRELEASED and invisible —
    auto-verification never releases). The QC gate applies exactly as
    for a human verify. Audited with actor "system:auto-verify"."""
    codes = [c["concept"]["code"] for c in checked]
    if qc_blockers(session, codes):
        return False
    _set_status(report, "final")
    _set_status(order, "verified")
    specimen = _specimen_row(session, order)
    if specimen is not None:
        _set_status(specimen, "verified")
    session.flush()
    append_audit(session, actor="system:auto-verify",
                 action="lab.report.verify",
                 entity=f"DiagnosticReport/{report.resource_id}",
                 payload={"order_id": order.resource_id,
                          "auto_verified": True, "rule_ids": rule_ids})
    session.commit()
    return True


def _create_results(session: Session, user: User, order: FhirResource,
                      results, source: str) -> dict:
    """Create preliminary, unreleased-tagged Observations + DiagnosticReport
    for an order in received/in-process state, and move order + specimen to
    resulted. Used identically by manual entry and analyser ingest: ingest
    results are drafts in exactly the same way (no auto-release)."""
    current = (order.body or {}).get("status")
    if current not in ("received", "in-process"):
        raise HTTPException(
            status_code=409,
            detail=(f"Results can only be entered for an order whose specimen "
                    f"has been received (status is '{current}')"))
    # Bench guard (Phase 6B): microbiology + pathology orders are
    # resulted through their bench endpoints (staged culture reports /
    # the synoptic form), never through numeric result entry here.
    order_concept_pre = _order_code(session, order)
    if order_concept_pre.get("bench") in ("microbiology", "pathology"):
        raise HTTPException(
            status_code=422,
            detail=(f"Orders on the {order_concept_pre.get('bench')} bench "
                    "are resulted through their bench workflow "
                    "(/lab/micro/* or /lab/pathology/*), not numeric "
                    "result entry"))
    checked = _validate_results(session, order, results)
    # POCT single-step mode applies only when the order is flagged
    # poct AND the deployment has published the POCT workflow (a
    # profile pack does that at enable time). A flagged order without
    # the published workflow follows the standard two-step path
    # exactly — flag alone changes nothing.
    poct = _is_poct(order) and published_config(
        session, "workflow", POCT_WORKFLOW_KEY) is not None
    if poct:
        # POCT single-step entry releases immediately, so the QC gate
        # is checked BEFORE anything is written (the standard path
        # checks it at verify/release instead). A blocked POCT entry
        # is refused whole: no draft results are left behind.
        blockers = qc_blockers(session, [c["concept"]["code"] for c in checked])
        if blockers:
            raise HTTPException(status_code=409,
                                detail=_qc_block_message(blockers))
    order_concept = _order_code(session, order)
    subject_ref = (order.body or {}).get("subject", {}).get("reference")
    specimen = _specimen_row(session, order)
    now = _now_iso()

    observations = []
    any_critical = False
    for entry in checked:
        analyte, value, unit = entry["concept"], entry["value"], entry["unit"]
        obs_body = {
            "resourceType": "Observation",
            "id": str(uuid.uuid4()),
            "status": "preliminary",
            "category": [{"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                "code": "laboratory", "display": "Laboratory"}]}],
            "code": {"coding": [{"system": LOINC, "code": analyte["code"],
                                 "display": analyte.get("display")}],
                     "text": analyte.get("display")},
            "subject": {"reference": subject_ref},
            "basedOn": [{"reference": f"ServiceRequest/{order.resource_id}"}],
            "effectiveDateTime": now,
            "meta": {"tag": [dict(fhir.LAB_UNRELEASED_TAG)]},
        }
        if entry["qualitative"]:
            # Coded result (Phase 6B): stored as a valueString; no
            # numeric interpretation or reference range applies.
            obs_body["valueString"] = value
        else:
            interp_code = interpret_value(value, analyte)
            if _is_critical(interp_code):
                any_critical = True
            obs_body["valueQuantity"] = {"value": value, "unit": unit}
            if interp_code:
                obs_body["interpretation"] = [{
                    "coding": [{"system": INTERPRETATION_SYSTEM, "code": interp_code,
                                "display": _INTERPRETATION_TEXT[interp_code]}],
                    "text": _INTERPRETATION_TEXT[interp_code]}]
            rng = analyte.get("referenceRange") or {}
            if rng.get("low") is not None or rng.get("high") is not None:
                ref_range = {}
                if rng.get("low") is not None:
                    ref_range["low"] = {"value": rng["low"], "unit": unit}
                if rng.get("high") is not None:
                    ref_range["high"] = {"value": rng["high"], "unit": unit}
                obs_body["referenceRange"] = [ref_range]
        if specimen is not None:
            obs_body["specimen"] = {"reference": f"Specimen/{specimen.resource_id}"}
        if (order.body or {}).get("encounter"):
            obs_body["encounter"] = order.body["encounter"]
        observations.append(fhir.create_resource("Observation", obs_body, session, user))

    report_body = {
        "resourceType": "DiagnosticReport",
        "id": str(uuid.uuid4()),
        "status": "preliminary",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
            "code": "LAB", "display": "Laboratory"}]}],
        "code": {"coding": [{"system": LOINC, "code": order_concept["code"],
                             "display": order_concept.get("display")}],
                 "text": order_concept.get("display")},
        "subject": {"reference": subject_ref},
        "basedOn": [{"reference": f"ServiceRequest/{order.resource_id}"}],
        "effectiveDateTime": now,
        "result": [{"reference": f"Observation/{o['id']}"} for o in observations],
        "meta": {"tag": [dict(fhir.LAB_UNRELEASED_TAG)]},
    }
    if specimen is not None:
        report_body["specimen"] = [{"reference": f"Specimen/{specimen.resource_id}"}]
    if (order.body or {}).get("encounter"):
        report_body["encounter"] = order.body["encounter"]
    if any_critical:
        report_body["extension"] = [{"url": CRITICAL_EXTENSION_URL,
                                     "valueBoolean": True}]
    report = fhir.create_resource("DiagnosticReport", report_body, session, user)

    _set_status(order, "resulted")
    if specimen is not None:
        _set_status(specimen, "resulted")
    session.flush()
    append_audit(session, actor=user.username, action="lab.results.create",
                 entity=f"DiagnosticReport/{report['id']}",
                 payload={"order_id": order.resource_id, "source": source,
                          "result_count": len(observations),
                          "critical": any_critical})
    if poct:
        # POCT single-step (deployment-profile workflow): entry IS the
        # verify + release — the POCT workflow has no verify state.
        # The QC gate was enforced above, before any write.
        report_row = fhir._get_row("DiagnosticReport", report["id"], session)
        _release_now(session, user, report_row, order,
                     extra_payload={"poct_single_step": True})
        report = fhir._render(report_row)
        observations = [fhir._render(fhir._get_row("Observation", o["id"], session))
                        for o in observations]
    session.commit()
    # Post-analytical automation (Phase 6B), after the draft exists:
    # reflex rules may create flagged add-on orders; auto-verification
    # may verify (never release) a draft that passes every rule.
    reflex_orders = _fire_reflex_rules(session, user, order, report["id"],
                                       checked)
    auto_verified = False
    if not poct:
        applies, rule_ids = _auto_verify_decision(session, order, checked)
        if applies:
            report_row = fhir._get_row("DiagnosticReport", report["id"], session)
            auto_verified = _auto_verify(session, report_row, order,
                                         checked, rule_ids)
            if auto_verified:
                report = fhir._render(report_row)
    return {"report": report, "observations": observations,
            "critical": any_critical, "reflex_orders": reflex_orders,
            "auto_verified": auto_verified}


@router.post("/orders/{order_id}/results", status_code=201)
async def enter_results(order_id: str, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    order = _order_row(session, order_id)
    return _create_results(session, user, order, payload.get("results"),
                            source="manual")


# ---------------------------------------------------------------------------
# Analyser ingest: HL7 v2 ORU^R01 and CSV
# ---------------------------------------------------------------------------

def ingest_hl7_groups(session: Session, user: User,
                      groups: list[tuple[str, str | None, list[dict]]],
                      source_format: str = "hl7",
                      instrument_id: str | None = None) -> list[dict]:
    """Create DRAFT results for parsed ingest groups — the shared core
    of the HTTP analyser ingest below and the Phase 8 MLLP listener
    (app.interop routes inbound ORU^R01 through this exact function,
    so a result that arrives over the wire is indistinguishable from
    one posted to /lab/ingest: same validation, same draft/unreleased
    state, same audit). `groups` is [(order_id, patient_id, results)]
    as produced by app.hl7.parse_hl7_oru / parse_csv_results. Raises
    HTTPException(422) on any mismatch — nothing is ingested partially
    per group."""
    created = []
    for order_id, patient_id, results in groups:
        try:
            order = fhir._get_row("ServiceRequest", order_id, session)
        except HTTPException:
            raise HTTPException(
                status_code=422,
                detail=f"Ingest references unknown lab order '{order_id}'")
        subject_ref = (order.body or {}).get("subject", {}).get("reference")
        if patient_id and subject_ref != f"Patient/{patient_id}":
            raise HTTPException(
                status_code=422,
                detail=(f"Ingest patient '{patient_id}' does not match order "
                        f"{order_id}'s patient ({subject_ref})"))
        outcome = _create_results(session, user, order, results,
                                  source=f"ingest-{source_format}")
        if instrument_id is not None:
            report_row = fhir._get_row("DiagnosticReport",
                                       outcome["report"]["id"], session)
            body = dict(report_row.body or {})
            exts = [e for e in (body.get("extension") or [])
                    if not (isinstance(e, dict)
                            and e.get("url") == INSTRUMENT_EXTENSION)]
            exts.append({"url": INSTRUMENT_EXTENSION,
                         "valueString": str(instrument_id)})
            body["extension"] = exts
            report_row.body = body
            report_row.version += 1
            session.commit()
        created.append({"order_id": order_id,
                        "report_id": outcome["report"]["id"],
                        "result_count": len(outcome["observations"]),
                        "critical": outcome["critical"]})
    return created


def parse_csv_results(content: str) -> list[tuple[str, str | None, list[dict]]]:
    """CSV fallback analyser format: one row per result with headers
    order_id, code, value and optional patient_id, unit. Rows are grouped
    by order. Returns [(order_id, patient_id, [results])]."""
    reader = csv.DictReader(io.StringIO(content))
    fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])}
    missing = {"order_id", "code", "value"} - fields
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"CSV ingest is missing required column(s): {', '.join(sorted(missing))}")
    grouped: dict[str, tuple[str | None, list[dict]]] = {}
    order_sequence: list[str] = []
    for row in reader:
        order_id = (row.get("order_id") or "").strip()
        code = (row.get("code") or "").strip()
        raw_value = (row.get("value") or "").strip()
        if not order_id and not code and not raw_value:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"CSV value '{raw_value}' for code '{code}' is not numeric")
        if order_id not in grouped:
            grouped[order_id] = ((row.get("patient_id") or "").strip() or None, [])
            order_sequence.append(order_id)
        grouped[order_id][1].append(
            {"code": code, "value": value,
             "unit": (row.get("unit") or "").strip() or None})
    if not order_sequence:
        raise HTTPException(status_code=422, detail="CSV ingest contains no result rows")
    return [(oid, grouped[oid][0], grouped[oid][1]) for oid in order_sequence]


@router.post("/ingest", status_code=201)
async def ingest(request: Request, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.work"))):
    """Analyser ingest. Accepts {"format": "hl7"|"csv", "content": "..."}.
    Ingested results are DRAFTS (preliminary + unreleased tag) created
    through the exact same path as manual entry — verification and
    release remain separate human steps; nothing is auto-released."""
    payload = await _json_body(request)
    fmt = str(payload.get("format") or "").strip().lower()
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=422, detail="Ingest requires 'content'")
    # Instrument metadata (Phase 6B): an ingest may name the analyser
    # that produced the results; it must be a REGISTERED instrument
    # (app.lab_benches registry) and is recorded as metadata on each
    # created report — it changes nothing about the results.
    instrument_id = payload.get("instrument_id")
    if instrument_id is not None:
        from .models import LabInstrument
        known = session.scalars(
            select(LabInstrument).where(
                LabInstrument.instrument_id == str(instrument_id))).first()
        if known is None:
            raise HTTPException(
                status_code=422,
                detail=(f"Ingest names instrument '{instrument_id}', which "
                        "is not in the instrument registry"))
    if fmt == "hl7":
        groups = [parse_hl7_oru(content)]
    elif fmt == "csv":
        groups = parse_csv_results(content)
    else:
        raise HTTPException(status_code=422,
                            detail="Ingest 'format' must be 'hl7' or 'csv'")

    created = ingest_hl7_groups(session, user, groups, source_format=fmt,
                                instrument_id=instrument_id)
    return {"ingested": created,
            "report_count": len(created),
            "result_count": sum(c["result_count"] for c in created),
            "instrument_id": instrument_id}


# ---------------------------------------------------------------------------
# Verification queue, verify, release
# ---------------------------------------------------------------------------

def _report_is_critical(session: Session, report: FhirResource) -> bool:
    """A report is critical when it carries the critical-result
    extension or any of its observations interpreted LL/HH."""
    for ext in (report.body or {}).get("extension") or []:
        if isinstance(ext, dict) and ext.get("url") == CRITICAL_EXTENSION_URL \
                and ext.get("valueBoolean") is True:
            return True
    for obs in _result_observations(session, report):
        interp = (((obs.body or {}).get("interpretation") or [{}])[0]
                  .get("coding") or [{}])[0].get("code")
        if _is_critical(interp):
            return True
    return False


def _notification_record(session: Session,
                         report_id: str) -> LabCriticalNotification | None:
    return session.scalars(
        select(LabCriticalNotification).where(
            LabCriticalNotification.report_id == report_id)).first()


def _notification_out(session: Session, report: FhirResource,
                      critical: bool) -> dict:
    """Notification state for one report. No record yet = 'pending' for
    a critical report (the call is outstanding) and 'not-required' for
    a non-critical one; the record's own state rules once it exists."""
    record = _notification_record(session, report.resource_id)
    if record is None:
        return {"state": "pending" if critical else "not-required",
                "notified_to": None, "read_back": None, "notified_by": None,
                "notified_at": None, "acknowledged_by": None,
                "acknowledged_at": None}
    return {
        "state": record.state,
        "notified_to": record.notified_to,
        "read_back": record.read_back,
        "notified_by": record.notified_by,
        "notified_at": record.notified_at.isoformat() if record.notified_at else None,
        "acknowledged_by": record.acknowledged_by,
        "acknowledged_at": (record.acknowledged_at.isoformat()
                              if record.acknowledged_at else None),
    }


def _report_amendment(session: Session, report: FhirResource) -> dict | None:
    """Amendment linkage for one report (Phase 6B): whether this report
    amends an earlier one, whether a later version has amended it, and
    the recorded reason. None when the report was never amended."""
    amends = amended_by = reason = None
    for ext in (report.body or {}).get("extension") or []:
        if not isinstance(ext, dict):
            continue
        if ext.get("url") == AMENDS_EXTENSION:
            amends = (ext.get("valueReference") or {}).get("reference")
        elif ext.get("url") == AMENDED_BY_EXTENSION:
            amended_by = (ext.get("valueReference") or {}).get("reference")
        elif ext.get("url") == AMENDMENT_REASON_EXTENSION:
            reason = ext.get("valueString")
    if amends is None and amended_by is None:
        return None
    return {"amends": amends, "amended_by": amended_by, "reason": reason}


def _report_summary(session: Session, report: FhirResource,
                    order: FhirResource | None) -> dict:
    observations = _result_observations(session, report)
    patient_ref = (report.body or {}).get("subject", {}).get("reference")
    results = []
    delta_flags = []
    critical = False
    for obs in observations:
        body = obs.body or {}
        interp = ((body.get("interpretation") or [{}])[0].get("coding") or [{}])[0].get("code")
        if _is_critical(interp):
            critical = True
        quantity = body.get("valueQuantity") or {}
        value = quantity.get("value")
        if value is None:
            value = body.get("valueString")  # qualitative result (Phase 6B)
        results.append({
            "observation_id": obs.resource_id,
            "code": _code_of(body),
            "display": (body.get("code") or {}).get("text"),
            "value": value, "unit": quantity.get("unit"),
            "interpretation": interp, "status": body.get("status"),
        })
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            code = _code_of(body)
            concept = _concept(session, code) if code else None
            if concept is not None:
                flag = _delta_flag(session, patient_ref, concept, float(value))
                if flag is not None:
                    delta_flags.append(flag)
    codes = [r["code"] for r in results if r["code"]]
    order_status = (order.body or {}).get("status") if order else None
    amendment = _report_amendment(session, report)
    return {
        "report": fhir._render(report),
        "order_id": order.resource_id if order else None,
        "order_status": order_status,
        "stage": ("awaiting-verification" if order_status == "resulted"
                  else "awaiting-release" if order_status == "verified"
                  else order_status),
        "critical": critical,
        "qc_blocked": qc_blockers(session, codes),
        "notification": _notification_out(session, report, critical),
        "amended": amendment is not None,
        "amendment": amendment,
        "delta_flags": delta_flags,
        "results": results,
    }


@router.get("/verification-queue")
def verification_queue(session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.read"))):
    """Lab drafts awaiting a human step: reports whose order is resulted
    (awaiting verification) or verified (awaiting release), each with its
    results, critical flags and live QC-block state."""
    reports = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "DiagnosticReport")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = []
    for report in reports:
        order = _report_order(session, report)
        if order is None:
            continue
        if (order.body or {}).get("status") in ("resulted", "verified"):
            items.append(_report_summary(session, report, order))
    return {"items": items, "total": len(items)}


def _lab_report(session: Session, report_id: str) -> tuple[FhirResource, FhirResource]:
    report = fhir._get_row("DiagnosticReport", report_id, session)
    order = _report_order(session, report)
    if order is None:
        raise HTTPException(status_code=422,
                            detail=f"DiagnosticReport/{report_id} is not a lab "
                                   "report (no ServiceRequest in basedOn)")
    return report, order


@router.post("/reports/{report_id}/verify")
def verify_report(report_id: str, session: Session = Depends(get_db),
                  user: User = Depends(require_capability("lab.verify"))):
    """Clinical verification: report preliminary -> final, order resulted
    -> verified. Blocked (409) while the latest QC run for any resulted
    analyte has failed. The report remains unreleased (invisible to the
    patient and the inbox) until the separate release step."""
    report, order = _lab_report(session, report_id)
    if (report.body or {}).get("status") != "preliminary" or \
            (order.body or {}).get("status") != "resulted":
        raise HTTPException(
            status_code=409,
            detail=(f"Report {report_id} is not awaiting verification "
                    f"(report status '{(report.body or {}).get('status')}', "
                    f"order status '{(order.body or {}).get('status')}')"))
    _enforce_qc(session, report)
    _set_status(report, "final")
    _set_status(order, "verified")
    specimen = _specimen_row(session, order)
    if specimen is not None:
        _set_status(specimen, "verified")
    session.flush()
    append_audit(session, actor=user.username, action="lab.report.verify",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"order_id": order.resource_id, "verified_by": user.username})
    session.commit()
    return {"report": fhir._render(report), "order": fhir._render(order)}


def _release_now(session: Session, user: User, report: FhirResource,
                 order: FhirResource, extra_payload: dict | None = None) -> None:
    """The release mutation itself (no guards, no QC check, no
    commit): observations become final, the unreleased visibility tag
    is removed from report + observations (this is the moment results
    appear in the clinician inbox and the patient portal), and the
    order/specimen move to released. Shared by the governed release
    endpoint (which adds its guards + QC gate) and the POCT
    single-step path in _create_results (which checks QC before any
    write instead)."""
    now = _now_iso()
    for obs in _result_observations(session, report):
        body = dict(obs.body or {})
        body["status"] = "final"
        obs.body = body
        obs.status = "final"
        obs.version += 1
        _strip_unreleased(obs)
    report_body = dict(report.body or {})
    report_body["status"] = "final"
    report_body["issued"] = now
    report.body = report_body
    report.status = "final"
    report.version += 1
    _strip_unreleased(report)
    _set_status(order, "released")
    specimen = _specimen_row(session, order)
    if specimen is not None:
        _set_status(specimen, "released")
    session.flush()
    payload = {"order_id": order.resource_id, "released_by": user.username}
    if extra_payload:
        payload.update(extra_payload)
    append_audit(session, actor=user.username, action="lab.report.release",
                 entity=f"DiagnosticReport/{report.resource_id}",
                 payload=payload)
    # Phase 8: the release is an interoperability event — queue the
    # outbound ORU^R01 (and an ELR variant when a resulted analyte is
    # reportable) in THIS transaction (lazy import: app.interop imports
    # app.lab for inbound ingest, so the hook must not cycle at module
    # load). With no enabled endpoint configured this queues nothing.
    from .interop import enqueue_released_report
    enqueue_released_report(session, report, order)


@router.post("/reports/{report_id}/release")
def release_report(report_id: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.verify"))):
    """Release a verified report (the mutation lives in _release_now).
    QC gate enforced again — a QC failure recorded after verification
    still blocks."""
    report, order = _lab_report(session, report_id)
    if (order.body or {}).get("status") != "verified":
        raise HTTPException(
            status_code=409,
            detail=(f"Report {report_id} cannot be released before verification "
                    f"(order status '{(order.body or {}).get('status')}')"))
    _enforce_qc(session, report)
    _release_now(session, user, report, order)
    session.commit()
    return {"report": fhir._render(report), "order": fhir._render(order)}


def _strip_unreleased(row: FhirResource) -> None:
    body = dict(row.body or {})
    meta = dict(body.get("meta") or {})
    tags = [t for t in (meta.get("tag") or [])
            if not (isinstance(t, dict)
                    and t.get("system") == fhir.LAB_VISIBILITY_SYSTEM
                    and t.get("code") == fhir.LAB_UNRELEASED_CODE)]
    if tags:
        meta["tag"] = tags
    else:
        meta.pop("tag", None)
    body["meta"] = meta
    row.body = body


# ---------------------------------------------------------------------------
# Amended reports (Phase 6B): a released report, corrected as a new version
# ---------------------------------------------------------------------------

@router.post("/reports/{report_id}/amend", status_code=201)
async def amend_report(report_id: str, request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.verify"))):
    """Amend a RELEASED report: creates a NEW DiagnosticReport (status
    `amended`) that supersedes it. Corrected results (`results`, same
    shape as result entry) become new final Observations; unchanged
    results are referenced from the original — nothing is rewritten or
    deleted. The original gains an `amended-by` link, the new report an
    `amends` link + the mandatory reason, and the pair stays visible
    (with the amended flag) in the inbox and the patient portal. A
    report that has itself been amended cannot be amended again —
    amend the current version. Amending an unreleased report -> 409;
    a missing reason -> 422."""
    payload = await _json_body(request)
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(status_code=422,
                            detail="Amending a report requires 'reason'")
    report, order = _lab_report(session, report_id)
    if (order.body or {}).get("status") != "released":
        raise HTTPException(
            status_code=409,
            detail=(f"Report {report_id} has not been released (order status "
                    f"'{(order.body or {}).get('status')}'); only a released "
                    "report can be amended"))
    amendment = _report_amendment(session, report)
    if amendment and amendment.get("amended_by"):
        raise HTTPException(
            status_code=409,
            detail=(f"Report {report_id} was already amended by "
                    f"{amendment['amended_by']}; amend the current version "
                    "instead"))
    checked = []
    if payload.get("results") is not None:
        checked = _validate_results(session, order, payload.get("results"))
    subject_ref = (order.body or {}).get("subject", {}).get("reference")
    specimen = _specimen_row(session, order)
    now = _now_iso()
    corrected_codes = {c["concept"]["code"] for c in checked}
    result_refs = []
    any_critical = False
    # Unchanged results: reference the original observations as-is.
    for obs in _result_observations(session, report):
        if _code_of(obs.body or {}) not in corrected_codes:
            result_refs.append({"reference": f"Observation/{obs.resource_id}"})
            interp = (((obs.body or {}).get("interpretation") or [{}])[0]
                      .get("coding") or [{}])[0].get("code")
            if _is_critical(interp):
                any_critical = True
    # Corrected results: new final, untagged observations.
    for entry in checked:
        analyte, value = entry["concept"], entry["value"]
        obs_body = {
            "resourceType": "Observation", "id": str(uuid.uuid4()),
            "status": "final",
            "category": [{"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                "code": "laboratory", "display": "Laboratory"}]}],
            "code": {"coding": [{"system": LOINC, "code": analyte["code"],
                                 "display": analyte.get("display")}],
                     "text": analyte.get("display")},
            "subject": {"reference": subject_ref},
            "basedOn": [{"reference": f"ServiceRequest/{order.resource_id}"}],
            "effectiveDateTime": now,
        }
        if entry["qualitative"]:
            obs_body["valueString"] = value
        else:
            obs_body["valueQuantity"] = {"value": value, "unit": entry["unit"]}
            interp_code = interpret_value(value, analyte)
            if _is_critical(interp_code):
                any_critical = True
            if interp_code:
                obs_body["interpretation"] = [{
                    "coding": [{"system": INTERPRETATION_SYSTEM,
                                "code": interp_code,
                                "display": _INTERPRETATION_TEXT[interp_code]}],
                    "text": _INTERPRETATION_TEXT[interp_code]}]
        if specimen is not None:
            obs_body["specimen"] = {"reference": f"Specimen/{specimen.resource_id}"}
        rendered = fhir.create_resource("Observation", obs_body, session, user)
        result_refs.append({"reference": f"Observation/{rendered['id']}"})

    original_body = report.body or {}
    extensions = [e for e in (original_body.get("extension") or [])
                  if isinstance(e, dict) and e.get("url") not in (
                      AMENDS_EXTENSION, AMENDED_BY_EXTENSION,
                      AMENDMENT_REASON_EXTENSION, CRITICAL_EXTENSION_URL)]
    extensions.append({"url": AMENDS_EXTENSION,
                       "valueReference": {"reference": f"DiagnosticReport/{report_id}"}})
    extensions.append({"url": AMENDMENT_REASON_EXTENSION,
                       "valueString": reason.strip()})
    if any_critical:
        extensions.append({"url": CRITICAL_EXTENSION_URL, "valueBoolean": True})
    new_report_body = {
        "resourceType": "DiagnosticReport", "id": str(uuid.uuid4()),
        "status": "amended",
        "category": original_body.get("category"),
        "code": original_body.get("code"),
        "subject": {"reference": subject_ref},
        "basedOn": [{"reference": f"ServiceRequest/{order.resource_id}"}],
        "effectiveDateTime": now, "issued": now,
        "result": result_refs,
        "extension": extensions,
    }
    if specimen is not None:
        new_report_body["specimen"] = [
            {"reference": f"Specimen/{specimen.resource_id}"}]
    if original_body.get("encounter"):
        new_report_body["encounter"] = original_body["encounter"]
    if payload.get("conclusion"):
        new_report_body["conclusion"] = str(payload["conclusion"])
    new_report = fhir.create_resource("DiagnosticReport", new_report_body,
                                      session, user)
    # Link the original forward (its content is otherwise untouched).
    body = dict(report.body or {})
    exts = [e for e in (body.get("extension") or [])
            if not (isinstance(e, dict) and e.get("url") == AMENDED_BY_EXTENSION)]
    exts.append({"url": AMENDED_BY_EXTENSION,
                 "valueReference": {"reference": f"DiagnosticReport/{new_report['id']}"}})
    body["extension"] = exts
    report.body = body
    report.version += 1
    session.flush()
    append_audit(session, actor=user.username, action="lab.report.amend",
                 entity=f"DiagnosticReport/{new_report['id']}",
                 payload={"original_report_id": report_id,
                          "order_id": order.resource_id,
                          "reason": reason.strip(),
                          "corrected_codes": sorted(corrected_codes)})
    session.commit()
    return {"report": new_report, "original_report_id": report_id,
            "order": fhir._render(order)}


# ---------------------------------------------------------------------------
# Critical-result notification (released critical reports)
# ---------------------------------------------------------------------------

@router.get("/critical-reports")
def critical_reports(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.read"))):
    """The critical-results worklist: RELEASED reports containing a
    critical (LL/HH) value, each with its notification state
    (pending -> notified -> acknowledged). Staff-only like every /lab
    surface; patients never see notification internals — their portal
    shows the released results themselves, and this record is not part
    of the FHIR report body."""
    reports = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "DiagnosticReport")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = []
    for report in reports:
        order = _report_order(session, report)
        if order is None or (order.body or {}).get("status") != "released":
            continue
        summary = _report_summary(session, report, order)
        if summary["critical"]:
            items.append(summary)
    return {"items": items, "total": len(items)}


@router.post("/reports/{report_id}/notify")
async def notify_critical_report(report_id: str, request: Request,
                                 session: Session = Depends(get_db),
                                 user: User = Depends(require_capability("lab.verify"))):
    """Record the critical-value phone notification for a RELEASED
    critical report: who was notified (`notified_to`) and the read-back
    confirmation (`read_back` must be true — the receiver reads the
    result back; without it the notification is not recorded, 422).
    Notifying a non-critical report -> 400; an unreleased one -> 409;
    notifying twice -> 409."""
    payload = await _json_body(request)
    report, order = _lab_report(session, report_id)
    if not _report_is_critical(session, report):
        raise HTTPException(
            status_code=400,
            detail=(f"Report {report_id} contains no critical value; "
                    "notification tracking applies to critical reports only"))
    if (order.body or {}).get("status") != "released":
        raise HTTPException(
            status_code=409,
            detail=(f"Report {report_id} has not been released (order status "
                    f"'{(order.body or {}).get('status')}'); critical "
                    "notification is recorded for released reports"))
    notified_to = payload.get("notified_to")
    if not isinstance(notified_to, str) or not notified_to.strip():
        raise HTTPException(
            status_code=422,
            detail="Notification requires 'notified_to' (who was notified)")
    if payload.get("read_back") is not True:
        raise HTTPException(
            status_code=422,
            detail=("Critical-value notification requires read-back "
                    "confirmation: 'read_back' must be true (the receiver "
                    "read the result back)"))
    record = _notification_record(session, report_id)
    if record is not None and record.state in ("notified", "acknowledged"):
        raise HTTPException(
            status_code=409,
            detail=(f"Report {report_id} was already notified "
                    f"(state '{record.state}')"))
    if record is None:
        record = LabCriticalNotification(report_id=report_id,
                                         order_id=order.resource_id)
        session.add(record)
    record.state = "notified"
    record.notified_to = notified_to.strip()
    record.read_back = True
    record.notified_by = user.username
    record.notified_at = datetime.now(timezone.utc)
    session.flush()
    append_audit(session, actor=user.username, action="lab.report.notify",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"order_id": order.resource_id,
                          "notified_to": record.notified_to,
                          "read_back": True})
    session.commit()
    return {"report_id": report_id,
            "notification": _notification_out(session, report, True)}


@router.post("/reports/{report_id}/acknowledge")
def acknowledge_critical_report(report_id: str,
                                session: Session = Depends(get_db),
                                user: User = Depends(require_capability("lab.verify"))):
    """Acknowledge a notified critical report (the receiving clinician
    confirms receipt). Acknowledging before notification -> 409; the
    state machine is pending -> notified -> acknowledged, in order."""
    report, order = _lab_report(session, report_id)
    record = _notification_record(session, report_id)
    if record is None or record.state == "pending":
        raise HTTPException(
            status_code=409,
            detail=(f"Report {report_id} has not been notified yet; "
                    "acknowledgement follows the notification call"))
    if record.state == "acknowledged":
        raise HTTPException(
            status_code=409,
            detail=f"Report {report_id} has already been acknowledged")
    record.state = "acknowledged"
    record.acknowledged_by = user.username
    record.acknowledged_at = datetime.now(timezone.utc)
    session.flush()
    append_audit(session, actor=user.username, action="lab.report.acknowledge",
                 entity=f"DiagnosticReport/{report_id}",
                 payload={"order_id": order.resource_id,
                          "acknowledged_by": user.username})
    session.commit()
    return {"report_id": report_id,
            "notification": _notification_out(session, report, True)}


# ---------------------------------------------------------------------------
# QC endpoints
# ---------------------------------------------------------------------------

@router.post("/qc", status_code=201)
async def create_qc_run(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    """Record a QC run. `passed` is computed here from the expected range
    — a caller-supplied verdict is never trusted."""
    payload = await _json_body(request)
    code = payload.get("analyte_code")
    if not code or _concept(session, str(code)) is None:
        raise HTTPException(status_code=422,
                            detail=f"QC 'analyte_code' '{code}' is not in the "
                                   "published lab catalog")
    numbers = {}
    for field in ("expected_low", "expected_high", "measured_value"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422, detail=f"QC '{field}' must be a number")
        numbers[field] = float(value)
    if numbers["expected_low"] > numbers["expected_high"]:
        raise HTTPException(status_code=422,
                            detail="QC 'expected_low' must not exceed 'expected_high'")
    material = payload.get("control_material")
    if not isinstance(material, str) or not material.strip():
        raise HTTPException(status_code=422,
                            detail="QC requires 'control_material' (control lot/material)")
    level = payload.get("level")
    if level is not None and not isinstance(level, str):
        raise HTTPException(status_code=422, detail="QC 'level' must be a string")
    # Westgard-style programme evaluation (Phase 6B): flags + z-score
    # are computed here from the published programme config and stored
    # on the run; `passed` below keeps its original range meaning.
    flags, z_score, effective_level = _westgard_flags(
        session, str(code), material, level, numbers["measured_value"])
    run = LabQcRun(
        analyte_code=str(code), control_material=material,
        expected_low=numbers["expected_low"], expected_high=numbers["expected_high"],
        measured_value=numbers["measured_value"],
        unit=payload.get("unit") if isinstance(payload.get("unit"), str) else None,
        passed=numbers["expected_low"] <= numbers["measured_value"] <= numbers["expected_high"],
        performed_by=user.username,
        level=effective_level, flags=flags, z_score=z_score)
    session.add(run)
    session.flush()
    append_audit(session, actor=user.username, action="lab.qc.run",
                 entity=f"LabQcRun/{run.id}",
                 payload={"analyte_code": run.analyte_code,
                          "control_material": run.control_material,
                          "measured_value": run.measured_value,
                          "passed": run.passed,
                          "level": run.level, "flags": run.flags,
                          "z_score": run.z_score})
    session.commit()
    return _qc_out(run)


@router.get("/qc/levey-jennings")
def levey_jennings(analyte_code: str = Query(...),
                   control_material: str = Query(...),
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    """Levey-Jennings data (Phase 6B): the QC run series for one
    analyte + control material, oldest first, with the series mean/SD
    computed server-side and each run's z-score against that series
    (plus the ±2SD / ±3SD control limits a chart is drawn from)."""
    import statistics
    runs = session.scalars(
        select(LabQcRun).where(LabQcRun.analyte_code == analyte_code,
                               LabQcRun.control_material == control_material)
        .order_by(LabQcRun.id.asc())).all()
    values = [r.measured_value for r in runs]
    mean = statistics.fmean(values) if values else None
    sd = statistics.pstdev(values) if len(values) > 1 else (0.0 if values else None)
    points = []
    for run in runs:
        z = ((run.measured_value - mean) / sd) if (mean is not None and sd) else None
        points.append({"id": run.id, "measured_value": run.measured_value,
                       "level": run.level, "flags": list(run.flags or []),
                       "passed": run.passed, "z_score": z,
                       "created_at": run.created_at.isoformat()
                       if run.created_at else None})
    return {"analyte_code": analyte_code, "control_material": control_material,
            "count": len(runs), "mean": mean, "sd": sd,
            "limits": ({"plus_2sd": mean + 2 * sd, "minus_2sd": mean - 2 * sd,
                        "plus_3sd": mean + 3 * sd, "minus_3sd": mean - 3 * sd}
                       if mean is not None and sd is not None else None),
            "runs": points}


@router.get("/qc")
def list_qc_runs(analyte_code: str | None = Query(default=None),
                 session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.read"))):
    stmt = select(LabQcRun).order_by(LabQcRun.id.desc())
    if analyte_code:
        stmt = stmt.where(LabQcRun.analyte_code == analyte_code)
    return [_qc_out(r) for r in session.scalars(stmt).all()]


@router.get("/qc/status")
def qc_status(analyte_code: str = Query(...), session: Session = Depends(get_db),
              user: User = Depends(require_capability("lab.read"))):
    latest = _latest_qc(session, analyte_code)
    return {"analyte_code": analyte_code,
            "blocked": bool(latest is not None and not latest.passed),
            "latest": _qc_out(latest) if latest else None}


# ---------------------------------------------------------------------------
# Catalog (effective Studio config, for lab clients)
# ---------------------------------------------------------------------------

@router.get("/catalog")
def lab_catalog(session: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    if user.role == Role.patient:
        raise HTTPException(status_code=403,
                            detail="The lab catalog is staff configuration data")
    catalog_cfg = published_config(session, "terminology", LAB_CATALOG_KEY)
    workflow_cfg = published_config(session, "workflow", LAB_WORKFLOW_KEY)
    micro_cfg = published_config(session, "workflow", MICRO_WORKFLOW_KEY)
    pathology_cfg = published_config(session, "workflow", PATHOLOGY_WORKFLOW_KEY)
    return {
        "catalog": catalog_cfg.payload if catalog_cfg else None,
        "catalog_version": catalog_cfg.version if catalog_cfg else None,
        "workflow": workflow_cfg.payload if workflow_cfg else None,
        "workflow_version": workflow_cfg.version if workflow_cfg else None,
        # Bench workflows (Phase 6B), for bench-aware clients.
        "micro_workflow": micro_cfg.payload if micro_cfg else None,
        "micro_workflow_version": micro_cfg.version if micro_cfg else None,
        "pathology_workflow": pathology_cfg.payload if pathology_cfg else None,
        "pathology_workflow_version":
            pathology_cfg.version if pathology_cfg else None,
    }
