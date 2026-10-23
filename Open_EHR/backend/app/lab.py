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
  releases; audited as actor "system:auto-verify") — extended by Lab
  Depth D7 (app.lab_autoverify): under a bench's PUBLISHED rule set a
  fully passing set auto-RELEASES with its rule trace, and Lab Depth
  D6 (app.lab_analyser) gates every ingest path on the reconciled
  instrument registry status (LAB-m2 quarantine) — delta checks flagged
  on the verification queue, reflex rules creating flagged draft orders,
  and amended reports (new linked version, original preserved).
- QC programme: control levels + Westgard-style flags computed at run
  creation from the published programme config, Levey-Jennings data API.
"""
import csv
import io
import math
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .hl7 import _hl7_component, _hl7_field  # noqa: F401
from .hl7 import parse_hl7_oru as _parse_hl7_oru_base  # noqa: F401
# The HL7 v2 reader lives in app.hl7 (the ONE shared HL7 module) since
# Phase 8; it is re-exported here so Phase 2 imports/tests are
# untouched. (parse_hl7_oru + its field/component helpers moved
# verbatim — see app.hl7.)
from .configstore import (load_workflow_machine, machine_from_payload,
                          payload_state_set, published_config,
                          transition_role_allowed, workflow_payload)
from .models import (AuditLog, ConfigStatus, FhirResource,
                     LabCriticalNotification, LabQcRun,
                     PoctCompetencyRecord, PoctQcRecord, Role, StudioConfig,
                     User, UserAccount)
from .security import get_db, get_current_user, require_capability
# Lab Depth W4 (LAB-D8/D9): the QC/calibration and reagent-lot
# modules. Imported at top level (they import app.lab only lazily,
# so there is no cycle) so their tables register on Base before
# create_all in EVERY import context — the result-entry, QC and
# ingest paths below call into them on ordinary lab requests.
from . import lab_analyser, lab_autoverify, lab_qc, lab_reagents  # noqa: E402,F401

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
# Depth pass: cytology reports (gyn + non-gyn) run on the pathology
# bench with their own published form; send-out routing and the POCT
# programme are published terminology configs like the catalog.
CYTOLOGY_FORM_KEY = "pathology-cytology"
SENDOUT_LABS_KEY = "sendout-labs"
SENDOUT_EXTENSION = "urn:opencare:extension/sendout"
POCT_PROGRAMME_KEY = "poct-programme"
# Lab Depth W9 (LAB-D15): the governed POCT DEPLOYMENT PROFILE —
# published terminology config owned by app.lab_poct_qms. When the
# profile publishes profileMode "enforce", its block flags turn the
# programme's seeded-off switches into enforced policy at POCT
# result entry (see _poct_effective_enforcement).
POCT_PROFILE_KEY = "poct-deployment-profile"
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

def default_blood_bank_gate() -> dict:
    """Wave A (LAB-B1): the governed blood-bank issue-gate parameters
    carried in the published lab catalog as `bloodBankGate`.

    Governed (site-configurable through Studio republication): how
    long a negative antibody screen stays valid, which products may
    use electronic crossmatch, and the emergency-release reason codes.
    NOT governed: the ABO/Rh compatibility matrices themselves — those
    are a clinical standard and live in code (app.lab_benches)."""
    return {
        "antibodyScreenValidityHours": 72,
        "electronicCrossmatch": {"enabled": True, "products": ["rbc"]},
        "emergencyReleaseReasonCodes": [
            {"code": "life-threatening-haemorrhage",
             "display": "Life-threatening haemorrhage — transfusion "
                        "cannot wait for a full crossmatch"},
            {"code": "massive-transfusion-protocol",
             "display": "Massive transfusion protocol activated"},
            {"code": "urgent-clinical-need",
             "display": "Urgent clinical need documented by the "
                        "treating clinician"},
        ],
    }


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
                bench="haematology", delta={"pct": 15, "windowHours": 168}),
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
                0.5, 1.1, critical_high=5.0, delta={"absolute": 0.3, "windowHours": 72}),
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
        # Depth pass — VIROLOGY bench. NAAT / antigen results are
        # qualitative on the standard spine; viral loads are numeric
        # and charted by the trend view (GET /lab/trends). Displays
        # marked "local code" are local codings (the BB-*/COAG-PANEL
        # precedent): a site maps them to its own LOINC set by
        # republishing the catalog.
        qualitative("94500-6", "SARS-CoV-2 RNA [Presence] in Respiratory specimen by NAA",
                    ["Detected", "Not detected", "Inconclusive"], "virology"),
        qualitative("VIR-INFA", "Influenza A RNA [Presence] by NAAT — local code",
                    ["Detected", "Not detected"], "virology"),
        qualitative("VIR-INFB", "Influenza B RNA [Presence] by NAAT — local code",
                    ["Detected", "Not detected"], "virology"),
        qualitative("VIR-RSV", "Respiratory syncytial virus RNA [Presence] by NAAT — local code",
                    ["Detected", "Not detected"], "virology"),
        {"code": "RVP-LOCAL",
         "display": "Respiratory viral panel (multiplex NAAT) — local grouping code",
         "active": True, "panel": True, "bench": "virology",
         "components": ["94500-6", "VIR-INFA", "VIR-INFB", "VIR-RSV"]},
        qualitative("HIV-AGAB", "HIV 1+2 antigen/antibody screen — local code",
                    ["Reactive", "Nonreactive"], "virology"),
        qualitative("HIV-DIFF", "HIV-1/HIV-2 antibody differentiation — local code",
                    ["HIV-1 positive", "HIV-2 positive", "Both positive",
                     "Negative", "Indeterminate"], "virology"),
        analyte("20447-9", "HIV 1 RNA [#/volume] in Blood by Probe (viral load)",
                "copies/mL", bench="virology"),
        analyte("42595-9", "Hepatitis B virus DNA [#/volume] in Serum or Plasma (viral load)",
                "IU/mL", bench="virology"),
        # Depth pass — SEROLOGY / IMMUNOLOGY bench (qualitative).
        qualitative("SER-HBSAG", "Hepatitis B surface antigen — local code",
                    ["Reactive", "Nonreactive"], "serology"),
        qualitative("SER-HCVAB", "Hepatitis C antibody — local code",
                    ["Reactive", "Nonreactive"], "serology"),
        qualitative("SER-RPR", "Syphilis screen (RPR) — local code",
                    ["Reactive", "Nonreactive"], "serology"),
        qualitative("SER-HAVIGM", "Hepatitis A IgM antibody — local code",
                    ["Reactive", "Nonreactive"], "serology"),
        # Depth pass — TOXICOLOGY bench: a qualitative urine drug
        # screen panel + numeric ethanol, all local codes.
        qualitative("TOX-COC", "Cocaine metabolite [Presence] in Urine — local code",
                    ["Positive", "Negative"], "toxicology"),
        qualitative("TOX-OPI", "Opiates [Presence] in Urine — local code",
                    ["Positive", "Negative"], "toxicology"),
        qualitative("TOX-THC", "Cannabinoids [Presence] in Urine — local code",
                    ["Positive", "Negative"], "toxicology"),
        qualitative("TOX-AMP", "Amphetamines [Presence] in Urine — local code",
                    ["Positive", "Negative"], "toxicology"),
        qualitative("TOX-BZO", "Benzodiazepines [Presence] in Urine — local code",
                    ["Positive", "Negative"], "toxicology"),
        {"code": "UDS-LOCAL",
         "display": "Urine drug screen panel — local grouping code",
         "active": True, "panel": True, "bench": "toxicology",
         "components": ["TOX-COC", "TOX-OPI", "TOX-THC", "TOX-AMP",
                        "TOX-BZO"]},
        analyte("TOX-ETOH", "Ethanol [Mass/volume] in Blood — local code",
                "mg/dL", bench="toxicology"),
        # Depth pass — CYTOLOGY on the pathology bench: the
        # `cytology` flag routes reporting to the published cytology
        # form (app.lab_benches) instead of the surgical synoptic.
        {"code": "10524-7", "display": "Cytology study of Cervix (Pap test)",
         "active": True, "bench": "pathology", "cytology": True},
        {"code": "CYTO-NONGYN",
         "display": "Non-gynaecologic cytology — local code",
         "active": True, "bench": "pathology", "cytology": True},
        # Depth pass — SEND-OUT tests: performed by a reference
        # laboratory; routing rides the published `sendout-labs`
        # terminology config, the bench workflow is the standard
        # spine + the send-out lifecycle in app.lab_benches.
        analyte("SO-METANEPH", "Plasma free metanephrines — local code (send-out)",
                "pg/mL", bench="sendout"),
        analyte("SO-VLCFA", "Very long-chain fatty acids — local code (send-out)",
                "umol/L", bench="sendout"),
    ]
    # Lab Depth D1 (compendium & directory): per-test DIRECTORY
    # attributes ride ON each concept as governed Studio data —
    # performing section/site, method, specimen requirements,
    # availability and per-priority TAT targets (each with a DECLARED
    # clock: "order-to-result" or "receipt-to-result", minutes) —
    # plus age/sex-partitioned reference ranges where a credible
    # partition exists. Shape (validated in
    # app.configstore.validate_terminology):
    #   directory: {performingSection, performingSite, method,
    #     specimen: {container, specimenType, minimumVolumeMl,
    #                fastingRequired},
    #     availability: {mode: "24-7"} |
    #                   {mode: "batched", batchSchedule:
    #                     {days: [mon..sun], times: ["HH:MM"]}} |
    #                   {mode: "send-out", referenceLab},
    #     tatTargets: [{priority, clock, targetMinutes}]}
    #   referenceRangePartitions: [{label, sex, ageMinYears,
    #     ageMaxYears, low, high, criticalLow, criticalHigh}]
    # Attributes a site cannot state credibly stay UNSET — an unset
    # specimen requirement never blocks ordering (only SET ones are
    # enforced at order placement, see _enforce_directory_requirements).
    def _tat(*entries):
        return [{"priority": p, "clock": c, "targetMinutes": m}
                for p, c, m in entries]

    def _std_tat():
        return _tat(("stat", "order-to-result", 60),
                    ("routine", "order-to-result", 240),
                    ("routine", "receipt-to-result", 180))

    def _dir(section, method, container=None, specimen_type=None,
             volume=None, fasting=None, availability=None, tat=None):
        specimen = {}
        if container is not None:
            specimen["container"] = container
        if specimen_type is not None:
            specimen["specimenType"] = specimen_type
        if volume is not None:
            specimen["minimumVolumeMl"] = volume
        if fasting is not None:
            specimen["fastingRequired"] = fasting
        entry = {"performingSection": section,
                 "performingSite": "Central laboratory",
                 "method": method,
                 "availability": availability or {"mode": "24-7"}}
        if specimen:
            entry["specimen"] = specimen
        if tat:
            entry["tatTargets"] = tat
        return entry

    _SST = ("Serum separator tube", "Serum", 3.5)
    _EDTA_WB = ("EDTA tube (lavender top)", "Whole blood", 2.0)
    _URINE = ("Sterile urine container", "Urine", 10.0)
    _BATCH_WEEKDAY = {"mode": "batched", "batchSchedule": {
        "days": ["mon", "tue", "wed", "thu", "fri"], "times": ["08:00"]}}
    directory_seed = {
        # Chemistry
        "2345-7": _dir("Chemistry", "Hexokinase (enzymatic)", *_SST,
                       tat=_std_tat()),
        "3094-0": _dir("Chemistry", "Urease (enzymatic)", *_SST,
                       tat=_std_tat()),
        "2160-0": _dir("Chemistry", "Enzymatic (creatininase)", *_SST,
                       tat=_std_tat()),
        "2951-2": _dir("Chemistry", "Ion-selective electrode (indirect)",
                       *_SST, tat=_std_tat()),
        "2823-3": _dir("Chemistry", "Ion-selective electrode (indirect)",
                       *_SST, tat=_std_tat()),
        "51990-0": _dir("Chemistry", "Automated chemistry analyser panel",
                        *_SST, tat=_std_tat()),
        "4548-4": _dir("Chemistry", "HPLC (cation-exchange)", *_EDTA_WB,
                       availability={"mode": "batched", "batchSchedule": {
                           "days": ["mon", "tue", "wed", "thu", "fri", "sat"],
                           "times": ["07:00"]}},
                       tat=_tat(("routine", "order-to-result", 1440))),
        "3016-3": _dir("Chemistry", "Chemiluminescent immunoassay", *_SST,
                       tat=_tat(("routine", "order-to-result", 360))),
        "3024-7": _dir("Chemistry", "Chemiluminescent immunoassay", *_SST,
                       tat=_tat(("routine", "order-to-result", 360))),
        # Haematology
        "718-7": _dir("Haematology",
                      "Automated haematology analyser (SLS photometry)",
                      *_EDTA_WB, tat=_std_tat()),
        "4544-3": _dir("Haematology",
                       "Automated haematology analyser (impedance)",
                       *_EDTA_WB, tat=_std_tat()),
        "6690-2": _dir("Haematology", "Automated haematology analyser",
                       *_EDTA_WB, tat=_std_tat()),
        "777-3": _dir("Haematology", "Automated haematology analyser",
                      *_EDTA_WB, tat=_std_tat()),
        "58410-2": _dir("Haematology", "Automated haematology analyser panel",
                        *_EDTA_WB, tat=_std_tat()),
        # Coagulation
        "5902-2": _dir("Coagulation", "Photo-optical clot detection",
                       "3.2% sodium citrate tube", "Plasma", 2.7,
                       tat=_std_tat()),
        "6301-6": _dir("Coagulation",
                       "Derived from PT (photo-optical clot detection)",
                       "3.2% sodium citrate tube", "Plasma", 2.7,
                       tat=_std_tat()),
        "3173-2": _dir("Coagulation", "Photo-optical clot detection",
                       "3.2% sodium citrate tube", "Plasma", 2.7,
                       tat=_std_tat()),
        "COAG-PANEL": _dir("Coagulation", "Photo-optical clot detection",
                           "3.2% sodium citrate tube", "Plasma", 2.7,
                           tat=_std_tat()),
        # Urinalysis (dipstick components + pH + specific gravity)
        "24356-8": _dir("Urinalysis",
                        "Automated urine analyser (reflectance photometry)",
                        *_URINE, tat=_tat(("routine", "order-to-result", 120))),
        # Microbiology
        "87040-1": _dir("Microbiology",
                        "Automated continuous-monitoring blood culture system",
                        "Blood culture bottle set (aerobic + anaerobic)",
                        "Blood", 20.0,
                        tat=_tat(("routine", "order-to-result", 4320))),
        "87086-6": _dir("Microbiology", "Culture on solid media",
                        "Sterile urine container", "Urine", 1.0,
                        tat=_tat(("routine", "order-to-result", 2880))),
        "87070-0": _dir("Microbiology", "Culture on solid media",
                        "Sterile transport swab", "Wound swab",
                        tat=_tat(("routine", "order-to-result", 2880))),
        # Blood bank
        "883-9": _dir("Blood bank", "Column agglutination",
                      "EDTA tube (blood bank)", "Whole blood", 3.0,
                      tat=_std_tat()),
        "890-4": _dir("Blood bank", "Column agglutination",
                      "EDTA tube (blood bank)", "Whole blood", 3.0,
                      tat=_std_tat()),
        "BB-TS": _dir("Blood bank", "Column agglutination",
                      "EDTA tube (blood bank)", "Whole blood", 3.0,
                      tat=_std_tat()),
        "BB-XM": _dir("Blood bank", "Column agglutination",
                      "EDTA tube (blood bank)", "Whole blood", 3.0,
                      tat=_std_tat()),
        # Pathology / cytology
        "11529-5": _dir("Pathology", "Histopathology (H&E)",
                        "Formalin container (10% neutral buffered formalin)",
                        "Tissue", availability=_BATCH_WEEKDAY,
                        tat=_tat(("routine", "order-to-result", 4320))),
        "10524-7": _dir("Pathology", "Cytopathology (liquid-based)",
                        "Liquid-based cytology vial", "Cervical sample",
                        availability=_BATCH_WEEKDAY,
                        tat=_tat(("routine", "order-to-result", 7200))),
        "CYTO-NONGYN": _dir("Pathology", "Cytopathology (liquid-based)",
                            "Liquid-based cytology vial", "Cytology sample",
                            availability=_BATCH_WEEKDAY,
                            tat=_tat(("routine", "order-to-result", 7200))),
        # Virology
        "94500-6": _dir("Virology", "Real-time PCR (NAAT)",
                        "Viral transport medium tube", "Nasopharyngeal swab",
                        tat=_tat(("stat", "order-to-result", 120),
                                 ("routine", "order-to-result", 360))),
        "VIR-INFA": _dir("Virology", "Real-time PCR (NAAT)",
                         "Viral transport medium tube",
                         "Nasopharyngeal swab",
                         tat=_tat(("stat", "order-to-result", 120),
                                  ("routine", "order-to-result", 360))),
        "VIR-INFB": _dir("Virology", "Real-time PCR (NAAT)",
                         "Viral transport medium tube",
                         "Nasopharyngeal swab",
                         tat=_tat(("stat", "order-to-result", 120),
                                  ("routine", "order-to-result", 360))),
        "VIR-RSV": _dir("Virology", "Real-time PCR (NAAT)",
                        "Viral transport medium tube",
                        "Nasopharyngeal swab",
                        tat=_tat(("stat", "order-to-result", 120),
                                 ("routine", "order-to-result", 360))),
        "RVP-LOCAL": _dir("Virology", "Real-time PCR (multiplex NAAT)",
                          "Viral transport medium tube",
                          "Nasopharyngeal swab",
                          tat=_tat(("stat", "order-to-result", 120),
                                   ("routine", "order-to-result", 360))),
        "HIV-AGAB": _dir("Virology", "Chemiluminescent immunoassay", *_SST,
                         tat=_tat(("routine", "order-to-result", 360))),
        "HIV-DIFF": _dir("Virology",
                         "Immunochromatographic differentiation assay",
                         *_SST, availability=_BATCH_WEEKDAY,
                         tat=_tat(("routine", "order-to-result", 2880))),
        "20447-9": _dir("Virology", "Real-time PCR",
                        "EDTA tube", "Plasma", 4.0,
                        availability={"mode": "batched", "batchSchedule": {
                            "days": ["mon", "wed", "fri"],
                            "times": ["08:00"]}},
                        tat=_tat(("routine", "order-to-result", 4320))),
        "42595-9": _dir("Virology", "Real-time PCR",
                        "EDTA tube", "Plasma", 4.0,
                        availability={"mode": "batched", "batchSchedule": {
                            "days": ["mon", "wed", "fri"],
                            "times": ["08:00"]}},
                        tat=_tat(("routine", "order-to-result", 4320))),
        # Serology / immunology
        "SER-HBSAG": _dir("Serology", "Chemiluminescent immunoassay", *_SST,
                          tat=_tat(("routine", "order-to-result", 360))),
        "SER-HCVAB": _dir("Serology", "Chemiluminescent immunoassay", *_SST,
                          tat=_tat(("routine", "order-to-result", 360))),
        "SER-RPR": _dir("Serology", "Flocculation (non-treponemal)", *_SST,
                        availability=_BATCH_WEEKDAY,
                        tat=_tat(("routine", "order-to-result", 1440))),
        "SER-HAVIGM": _dir("Serology", "Chemiluminescent immunoassay",
                           *_SST, availability=_BATCH_WEEKDAY,
                           tat=_tat(("routine", "order-to-result", 1440))),
        # Toxicology
        "TOX-COC": _dir("Toxicology", "Enzyme immunoassay",
                        "Sterile urine container", "Urine", 5.0,
                        tat=_tat(("stat", "order-to-result", 120),
                                 ("routine", "order-to-result", 240))),
        "TOX-OPI": _dir("Toxicology", "Enzyme immunoassay",
                        "Sterile urine container", "Urine", 5.0,
                        tat=_tat(("stat", "order-to-result", 120),
                                 ("routine", "order-to-result", 240))),
        "TOX-THC": _dir("Toxicology", "Enzyme immunoassay",
                        "Sterile urine container", "Urine", 5.0,
                        tat=_tat(("stat", "order-to-result", 120),
                                 ("routine", "order-to-result", 240))),
        "TOX-AMP": _dir("Toxicology", "Enzyme immunoassay",
                        "Sterile urine container", "Urine", 5.0,
                        tat=_tat(("stat", "order-to-result", 120),
                                 ("routine", "order-to-result", 240))),
        "TOX-BZO": _dir("Toxicology", "Enzyme immunoassay",
                        "Sterile urine container", "Urine", 5.0,
                        tat=_tat(("stat", "order-to-result", 120),
                                 ("routine", "order-to-result", 240))),
        "UDS-LOCAL": _dir("Toxicology", "Enzyme immunoassay panel",
                          "Sterile urine container", "Urine", 5.0,
                          tat=_tat(("stat", "order-to-result", 120),
                                   ("routine", "order-to-result", 240))),
        "TOX-ETOH": _dir("Toxicology",
                         "Enzymatic (alcohol dehydrogenase)",
                         "Sodium fluoride / potassium oxalate tube",
                         "Whole blood", 2.0,
                         tat=_tat(("stat", "order-to-result", 60),
                                  ("routine", "order-to-result", 240))),
        # Send-out (performed by a reference laboratory)
        "SO-METANEPH": _dir("Send-out", "LC-MS/MS (reference laboratory)",
                            "EDTA tube", "Plasma", 2.0,
                            availability={"mode": "send-out",
                                          "referenceLab":
                                          "Regional reference laboratory"},
                            tat=_tat(("routine", "order-to-result", 7200))),
        "SO-VLCFA": _dir("Send-out", "GC-MS (reference laboratory)",
                         "Serum separator tube", "Serum", 3.5,
                         availability={"mode": "send-out",
                                       "referenceLab":
                                       "National specialty reference laboratory"},
                         tat=_tat(("routine", "order-to-result", 10080))),
    }
    for code in ("5792-7", "5804-0", "5797-6", "5794-3", "5799-2",
                 "5802-4", "5770-3", "5803-2", "5811-5"):
        directory_seed[code] = _dir(
            "Urinalysis",
            "Automated urine analyser (reflectance photometry)",
            *_URINE, tat=_tat(("routine", "order-to-result", 120)))
    # Age/sex-partitioned reference ranges (adult partitions; the
    # default range stays the fallback for patients whose age/sex is
    # unknown or outside every partition).
    partitions_seed = {
        "718-7": [  # Haemoglobin: adult male / adult female
            {"label": "Adult male", "sex": "male", "ageMinYears": 18,
             "ageMaxYears": None, "low": 13.0, "high": 17.0,
             "criticalLow": 7.0, "criticalHigh": 20.0},
            {"label": "Adult female", "sex": "female", "ageMinYears": 18,
             "ageMaxYears": None, "low": 12.0, "high": 15.5,
             "criticalLow": 7.0, "criticalHigh": 20.0},
        ],
        "4544-3": [  # Haematocrit: adult male / adult female
            {"label": "Adult male", "sex": "male", "ageMinYears": 18,
             "ageMaxYears": None, "low": 40.0, "high": 50.0,
             "criticalLow": 20.0, "criticalHigh": 60.0},
            {"label": "Adult female", "sex": "female", "ageMinYears": 18,
             "ageMaxYears": None, "low": 36.0, "high": 46.0,
             "criticalLow": 20.0, "criticalHigh": 60.0},
        ],
        "2160-0": [  # Creatinine: adult male / adult female
            {"label": "Adult male", "sex": "male", "ageMinYears": 18,
             "ageMaxYears": None, "low": 0.7, "high": 1.3},
            {"label": "Adult female", "sex": "female", "ageMinYears": 18,
             "ageMaxYears": None, "low": 0.5, "high": 1.1},
        ],
    }
    for concept in concepts:
        entry = directory_seed.get(concept.get("code"))
        if entry is not None:
            concept["directory"] = entry
        parts = partitions_seed.get(concept.get("code"))
        if parts is not None:
            concept["referenceRangePartitions"] = parts
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
                                 "reject2_2s": True, "rejectR_4s": True,
                                 "reject4_1s": True, "reject10x": True}}},
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
                # Depth pass: qualitative reflex — a Reactive HIV
                # screen reflexes the confirmatory differentiation
                # assay (the standard screen -> confirm algorithm).
                {"id": "hiv-reactive-add-diff", "triggerCode": "HIV-AGAB",
                 "when": "qualitative-match", "matchValue": "Reactive",
                 "addCode": "HIV-DIFF",
                 "note": "Reactive HIV screen reflexes the HIV-1/2 "
                         "differentiation assay"},
            ],
            # Auto-verification (Phase 6B): OFF by default — seeded
            # behaviour is unchanged (every draft awaits a human verify).
            # Publishing enabled=true turns it on: a draft then
            # auto-verifies only when EVERY resulted analyte carries an
            # `autoVerify` range in this catalog, the value is inside
            # it, not critical, not QC-blocked, and within its delta
            # bounds when configured. It NEVER auto-releases.
            "autoVerify": {"enabled": False},
            # Stewardship / utilisation (Depth pass): placing an
            # order for the same patient + analyte while another
            # order for it exists inside the window returns a
            # duplicate-order WARNING alongside the created order —
            # it never blocks (the Phase 5 CPOE pattern). Sites tune
            # the window or disable the check by republishing.
            "stewardship": {"duplicateCheckEnabled": True,
                            "duplicateWindowHours": 24},
            # Blood-bank product types (Phase 6B): unit inventory
            # validates product_type against this list; blood groups
            # validate against the ABO concept's `values` above.
            "bloodProducts": [
                {"code": "rbc", "display": "Red blood cells"},
                {"code": "plasma", "display": "Fresh frozen plasma"},
                {"code": "platelets", "display": "Platelets"},
                {"code": "cryoprecipitate", "display": "Cryoprecipitate"},
            ],
            # Wave A (LAB-B1): blood-bank issue-gate parameters — see
            # default_blood_bank_gate(). Existing published catalogs
            # gain this section through _extend_blood_bank_gate.
            "bloodBankGate": default_blood_bank_gate()}


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
        # LAB-D12: `mdro` / `reportable` are the governed MDRO flags
        # app.lab_micro's finalisation alert reads. Seeded False for
        # every organism (OFF): flagging an organism as an MDRO /
        # reportable alert trigger is the laboratory's governed
        # go-live decision, made by republishing this terminology.
        return {"code": code, "display": display, "active": True,
                "mdro": False, "reportable": False}
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


def default_cytology_form_payload() -> dict:
    """Cytology report form (Depth pass) — a published Studio FORM
    completed through the Phase 5 documentation machinery, the
    cytology counterpart of the surgical synoptic form. One form
    serves gyn (Pap) and non-gyn cytology; the specimen type item
    carries which."""
    def item(link_id, text, type_, required=False, options=None):
        out = {"linkId": link_id, "text": text, "type": type_,
               "required": required}
        if options:
            out["answerOption"] = [
                {"valueCoding": {"code": o, "display": o}} for o in options]
        return out
    return {
        "resourceType": "Questionnaire",
        "title": "Cytology report",
        "item": [
            item("specimenType", "Specimen type / source", "string", True),
            item("adequacy", "Specimen adequacy", "choice", True,
                 ["Satisfactory", "Satisfactory but limited",
                  "Unsatisfactory"]),
            item("category", "General category", "choice", True,
                 ["Negative", "Atypical", "Suspicious",
                  "Positive for malignancy", "Non-diagnostic"]),
            item("interpretation", "Interpretation / diagnosis", "string", True),
            item("comment", "Cytopathologist comment", "string"),
        ],
    }


def default_sendout_labs_payload() -> dict:
    """Send-out reference laboratories (Depth pass) — published
    terminology config. A catalog test is a send-out test when a
    laboratory's `tests` list names its code; `tatHours` is the
    laboratory's contracted turnaround the send-out TAT view measures
    against. Sites replace these synthetic laboratories with their
    real reference-lab contracts by republishing."""
    return {"name": "OpenCare send-out reference laboratories",
            # The laboratories also ride as `concepts` so the document
            # stays a valid, republishable terminology config through
            # the governed API (the Phase 14 devices pattern); `labs`
            # is the operative routing list the module reads.
            "concepts": [
                {"code": "ref-regional",
                 "display": "Regional reference laboratory",
                 "active": True},
                {"code": "ref-specialty",
                 "display": "National specialty reference laboratory",
                 "active": True},
            ],
            "labs": [
                {"code": "ref-regional",
                 "display": "Regional reference laboratory",
                 "tests": ["SO-METANEPH"], "tatHours": 120,
                 # LAB-D5 (catalog-as-contract): the performing
                 # laboratory's identity, stamped onto received
                 # reports, and the per-test contract enforced at
                 # manifest packing by app.lab_preanalytical.
                 "performingLab": {
                     "code": "ref-regional",
                     "name": "Regional Reference Laboratory (synthetic)",
                     "cliaNumber": "00D0000000"},
                 "contracts": {
                     "SO-METANEPH": {
                         "stability": {"hours": 72, "condition": "refrigerated"},
                         "shippingTemperature": "refrigerated",
                         "splitBeforeTransport": (
                             "Separate plasma within 4 hours of collection "
                             "and ship the labelled aliquot; freeze if "
                             "transit will exceed 24 hours"),
                         "requiredAttachments": ["requisition-form",
                                                 "clinical-history"],
                         "reportWindowHours": 120,
                         "classification": "FDA-cleared"}}},
                {"code": "ref-specialty",
                 "display": "National specialty reference laboratory",
                 "tests": ["SO-VLCFA"], "tatHours": 240,
                 "performingLab": {
                     "code": "ref-specialty",
                     "name": "National Specialty Reference Laboratory (synthetic)",
                     "cliaNumber": "00D0000001"},
                 "contracts": {
                     "SO-VLCFA": {
                         "stability": {"hours": 168, "condition": "frozen"},
                         "shippingTemperature": "frozen",
                         "splitBeforeTransport": (
                             "Separate serum before transport and ship "
                             "frozen on dry ice in the labelled aliquot"),
                         "requiredAttachments": ["requisition-form",
                                                 "consent-form"],
                         "reportWindowHours": 240,
                         "classification": "LDT"}}},
            ]}


def default_poct_programme_payload() -> dict:
    """POCT programme (Depth pass) — published terminology config
    governing point-of-care testing quality: which Phase 14 registry
    device types are POCT devices, the control levels run daily, the
    operator-competency validity window (CLIA competency model), and
    the enforcement switches. Enforcement ships OFF (the auto-verify
    precedent): the programme TRACKS QC + competency from day one; a
    site turns a block on by republishing once its operators are
    enrolled, and the flip is test-proven both ways."""
    return {"name": "OpenCare POCT programme",
            # The QC levels ride as `concepts` so the document stays
            # a valid, republishable terminology config through the
            # governed API (the Phase 14 devices pattern); `qcLevels`
            # is the operative list the module reads.
            "concepts": [
                {"code": "L1", "display": "Control level 1 (low)",
                 "active": True},
                {"code": "L2", "display": "Control level 2 (high)",
                 "active": True},
            ],
            "poctDeviceTypes": ["poct-analyser"],
            "qcLevels": ["L1", "L2"],
            "competencyValidityMonths": 12,
            "competencyExpiringWindowDays": 30,
            "enforcement": {"blockUncompetentOperator": False,
                            "blockMissingDailyQc": False}}


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


def _extend_blood_bank_gate(session: Session) -> None:
    """Wave A (LAB-B1): add the `bloodBankGate` section to a PUBLISHED
    lab catalog that predates it, as a new governed version — the
    inpatient._extend_note_types pattern. The existing payload
    (including any site customisation) is preserved verbatim; only
    the missing section is added, by the system actor, audited.
    Idempotent: a catalog already carrying the section is untouched."""
    cfg = published_config(session, "terminology", LAB_CATALOG_KEY)
    if cfg is None:
        return
    if isinstance((cfg.payload or {}).get("bloodBankGate"), dict):
        return
    payload = dict(cfg.payload or {})
    payload["bloodBankGate"] = default_blood_bank_gate()
    cfg.status = ConfigStatus.superseded
    session.flush()
    versions = session.scalars(
        select(StudioConfig).where(StudioConfig.kind == "terminology",
                                   StudioConfig.key == LAB_CATALOG_KEY)
        .order_by(StudioConfig.version.asc())).all()
    row = StudioConfig(kind="terminology", key=LAB_CATALOG_KEY,
                       version=(versions[-1].version + 1) if versions else 1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:terminology/{LAB_CATALOG_KEY}@v{row.version}",
                     payload={"kind": "terminology", "key": LAB_CATALOG_KEY,
                              "version": row.version,
                              "wave_a_extension": ["bloodBankGate"]})


def ensure_lab_defaults(session: Session) -> None:
    """Seed the lab catalog + lab workflow as published Studio config v1
    (idempotent), exactly like the ADT workflow seed in studio_config.
    Phase 6B adds the bench workflows + micro terminology + the
    pathology synoptic form, seeded the same way."""
    _seed_published(session, "terminology", LAB_CATALOG_KEY,
                    default_lab_catalog_payload())
    _extend_blood_bank_gate(session)
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
    # Depth pass: cytology form + send-out laboratories + POCT
    # programme, seeded the same governed way.
    _seed_published(session, "form", CYTOLOGY_FORM_KEY,
                    default_cytology_form_payload())
    _seed_published(session, "terminology", SENDOUT_LABS_KEY,
                    default_sendout_labs_payload())
    _seed_published(session, "terminology", POCT_PROGRAMME_KEY,
                    default_poct_programme_payload())
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


def blood_bank_gate_config(session: Session) -> dict:
    """The effective blood-bank issue-gate parameters (Wave A,
    LAB-B1): the published catalog's `bloodBankGate` section merged
    over the seeded defaults, so a site-published section that omits
    a key still yields a complete, safe configuration."""
    gate = default_blood_bank_gate()
    published = _catalog(session).get("bloodBankGate")
    if isinstance(published, dict):
        for key, value in published.items():
            if key == "electronicCrossmatch" and isinstance(value, dict):
                merged = dict(gate["electronicCrossmatch"])
                merged.update(value)
                gate["electronicCrossmatch"] = merged
            else:
                gate[key] = value
    return gate


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


def _poct_programme(session: Session) -> dict:
    """The published POCT programme config (empty dict when a site
    has unpublished it — tracking endpoints then report the programme
    as unconfigured and enforcement stays off)."""
    cfg = published_config(session, "terminology", POCT_PROGRAMME_KEY)
    return (cfg.payload or {}) if cfg is not None else {}


def _poct_deployment_profile(session: Session) -> dict:
    """The published POCT deployment profile (Lab Depth W9,
    LAB-D15; the payload default lives in app.lab_poct_qms). Empty
    dict when unpublished — the profile then enforces nothing and
    legacy programme behaviour is preserved exactly."""
    cfg = published_config(session, "terminology", POCT_PROFILE_KEY)
    return (cfg.payload or {}) if cfg is not None else {}


def _poct_effective_enforcement(session: Session,
                                programme: dict) -> dict:
    """Effective POCT enforcement at result entry (LAB-D15): the
    programme's own published switches, OR — when the deployment
    profile publishes profileMode "enforce" — the profile's block
    flags. The profile can only ADD a block, never remove a
    programme-published one; in "track-only" mode (the seeded
    default) the programme switches govern alone, exactly as
    before W9."""
    enforcement = dict(programme.get("enforcement") or {})
    profile = _poct_deployment_profile(session)
    if profile.get("profileMode") == "enforce":
        if profile.get("blockUncompetentOperator") is True:
            enforcement["blockUncompetentOperator"] = True
        if profile.get("blockMissingDailyQc") is True:
            enforcement["blockMissingDailyQc"] = True
    return enforcement


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def _competency_status(programme: dict,
                       assessed_on: str) -> tuple[str, str | None]:
    """(status, expires_on) for one competency assessment: current
    while today is within the published validity window of the
    assessment date. An unparseable date is 'unknown', never current."""
    try:
        assessed = date.fromisoformat(assessed_on)
    except (TypeError, ValueError):
        return "unknown", None
    months = programme.get("competencyValidityMonths")
    if not isinstance(months, int) or isinstance(months, bool) or months <= 0:
        months = 12
    expires = _add_months(assessed, months)
    return ("current" if date.today() <= expires else "expired"), \
        expires.isoformat()


def _competency_current(session: Session, programme: dict,
                        operator_ids: set[str], test_code: str) -> bool:
    """True when any of the operator's identities holds a CURRENT
    competency record for the test. Identities: the staff-directory
    id on the operator's Front Door account, plus their username."""
    if not operator_ids:
        return False
    rows = session.scalars(
        select(PoctCompetencyRecord).where(
            PoctCompetencyRecord.staff_id.in_(sorted(operator_ids)),
            PoctCompetencyRecord.test_code == test_code)).all()
    return any(_competency_status(programme, row.assessed_on)[0] == "current"
               for row in rows)


def _operator_ids(session: Session, user: User) -> set[str]:
    ids = {user.username}
    account = session.scalars(
        select(UserAccount).where(UserAccount.username == user.username)).first()
    if account is not None and account.staff_id:
        ids.add(account.staff_id)
    return ids


def _poct_qc_current(session: Session, programme: dict,
                     device_key: str) -> bool:
    """True when the device's LATEST QC run today passed at every
    level the programme requires. A later failure supersedes an
    earlier pass (the latest run is the device's state)."""
    levels = [lvl for lvl in (programme.get("qcLevels") or [])
              if isinstance(lvl, str)]
    if not levels:
        return True
    today = datetime.now(timezone.utc).date()
    rows = session.scalars(
        select(PoctQcRecord).where(PoctQcRecord.device_key == device_key)
        .order_by(PoctQcRecord.id.desc())).all()
    latest: dict[str, str] = {}
    for row in rows:
        if row.created_at is not None and row.created_at.date() == today \
                and row.level not in latest:
            latest[row.level] = row.result
    return all(latest.get(level) == "pass" for level in levels)


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


def patient_demographics(session: Session,
                         subject_ref: str | None) -> dict:
    """{age_years, sex} for a Patient reference, for partitioned
    reference-range selection (Lab Depth D1). Age is whole years at
    today (UTC — the server's "today" rule); sex is the FHIR
    Patient.gender lower-cased. Anything unknown stays None, which
    makes range selection fall back to the default range."""
    out: dict = {"age_years": None, "sex": None}
    if not subject_ref or not subject_ref.startswith("Patient/"):
        return out
    try:
        row = fhir._get_row("Patient", subject_ref.split("/", 1)[1], session)
    except HTTPException:
        return out
    body = row.body or {}
    gender = body.get("gender")
    if isinstance(gender, str) and gender.strip():
        out["sex"] = gender.strip().lower()
    birth = body.get("birthDate")
    if isinstance(birth, str) and birth.strip():
        try:
            born = date.fromisoformat(birth.strip()[:10])
        except ValueError:
            born = None
        if born is not None:
            today = datetime.now(timezone.utc).date()
            out["age_years"] = today.year - born.year - (
                (today.month, today.day) < (born.month, born.day))
    return out


def _demographics_for_order(session: Session,
                            order: FhirResource) -> dict:
    return patient_demographics(
        session, ((order.body or {}).get("subject") or {}).get("reference"))


def select_reference_range(concept: dict,
                           patient: dict | None = None) -> dict:
    """The effective reference range for a result (Lab Depth D1):
    when the concept carries `referenceRangePartitions` and the
    patient's age/sex match a partition, that partition's bounds
    override the default range's (fields the partition omits —
    e.g. critical thresholds — inherit from the default). First
    matching partition in published order wins. No partitions, no
    demographics, or no match -> the default range, unchanged."""
    rng = dict(concept.get("referenceRange") or {})
    partitions = concept.get("referenceRangePartitions")
    if not isinstance(partitions, list) or not patient:
        return rng
    age, sex = patient.get("age_years"), patient.get("sex")
    for part in partitions:
        if not isinstance(part, dict):
            continue
        p_sex = part.get("sex")
        if p_sex in ("male", "female") and sex != p_sex:
            continue
        age_min, age_max = part.get("ageMinYears"), part.get("ageMaxYears")
        if age_min is not None and (age is None or age < age_min):
            continue
        if age_max is not None and (age is None or age > age_max):
            continue
        for bound in ("low", "high", "criticalLow", "criticalHigh", "unit"):
            if part.get(bound) is not None:
                rng[bound] = part[bound]
        rng["partition"] = part.get("label")
        return rng
    return rng


def interpret_value(value: float, concept: dict,
                    patient: dict | None = None) -> str | None:
    """HL7 v3 ObservationInterpretation code for a numeric result against
    the analyte's configured range. Critical thresholds win over the
    reference range; returns None when the analyte has no range data.
    When `patient` demographics are supplied and the concept carries
    age/sex-partitioned ranges, the matching partition governs
    (Lab Depth D1); otherwise the default range — the pre-D1
    behaviour, byte-identical for concepts without partitions."""
    rng = select_reference_range(concept, patient)
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
        # Lab Depth W4 (LAB-D8/D9): the run's instrument + reagent
        # lot + shift keys and its control-lot identity (None on
        # pre-W4 rows, which carry none of them).
        "instrument_id": getattr(run, "instrument_id", None),
        "reagent_lot_key": getattr(run, "reagent_lot_key", None),
        "control_lot_id": getattr(run, "control_lot_id", None),
        "shift": getattr(run, "shift", None),
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
                    level: str | None, measured: float,
                    instrument_id: str | None = None,
                    reagent_lot_key: str | None = None) -> tuple[list[str], float | None, str | None]:
    """Westgard rule evaluation for one new QC run (Phase 6B, full
    set completed by Lab Depth W4 / LAB-D8).

    Looks the (material, analyte) up in the published programme config
    for its established mean/SD; computes z = (measured - mean) / sd
    and evaluates the FULL Westgard set against the run series keyed
    to analyte + material + level (+ instrument + reagent lot when
    the new run carries those keys — LAB-D8 keying):

    - "1-2s" (warning): |z| > 2.
    - "1-3s" (rejection): |z| > 3.
    - "2-2s" (rejection): the previous run in the series also beyond
      2 SD on the SAME side.
    - "R-4s" (rejection): the previous run in the series is beyond
      2 SD on the OPPOSITE side (the pair's range exceeds 4 SD).
    - "4-1s" (rejection): this run and the previous 3 in the series
      all beyond 1 SD on the same side.
    - "10x" (rejection): this run and the previous 9 in the series
      all on the same side of the mean.

    Every flag NAMES the rule that fired (the rule trace). Rules are
    switches in the same programme config (`warn1_2s`, `reject1_3s`,
    `reject2_2s`, `rejectR_4s`, `reject4_1s`, `reject10x`; absent =
    on). Returns (flags, z, level) — the level falls back to the
    material's configured level. No programme entry for the material:
    no flags, no z (the run still records, and `passed` still comes
    from the expected range)."""
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
    # The keyed series: previous runs of the same analyte + material
    # + level, narrowed to the same instrument / reagent lot when
    # the new run is keyed to them (pre-W4 runs carry no keys and
    # join only unkeyed series). Newest first, capped at the 9
    # predecessors the longest rule (10x) can look back over.
    stmt = select(LabQcRun).where(LabQcRun.analyte_code == analyte_code,
                                  LabQcRun.control_material == material)
    if instrument_id is not None:
        stmt = stmt.where(LabQcRun.instrument_id == instrument_id)
    if reagent_lot_key is not None:
        stmt = stmt.where(LabQcRun.reagent_lot_key == reagent_lot_key)
    series = [r for r in session.scalars(
        stmt.order_by(LabQcRun.id.desc()).limit(10)).all()
        if r.z_score is not None
        and (r.level or entry.get("level")) == effective_level]
    previous_z = [float(r.z_score) for r in series]

    def _same_side(values: list[float]) -> bool:
        return all(v > 0 for v in values) or all(v < 0 for v in values)

    if rules.get("reject2_2s", True) and abs(z) > 2 and previous_z \
            and abs(previous_z[0]) > 2 \
            and (previous_z[0] > 0) == (z > 0):
        flags.append("2-2s")
    if rules.get("rejectR_4s", True) and previous_z \
            and abs(z) > 2 and abs(previous_z[0]) > 2 \
            and (previous_z[0] > 0) != (z > 0) \
            and abs(z - previous_z[0]) > 4:
        flags.append("R-4s")
    if rules.get("reject4_1s", True) and len(previous_z) >= 3:
        window = [z] + previous_z[:3]
        if all(abs(v) > 1 for v in window) and _same_side(window):
            flags.append("4-1s")
    if rules.get("reject10x", True) and len(previous_z) >= 9:
        window = [z] + previous_z[:9]
        if all(v != 0 for v in window) and _same_side(window):
            flags.append("10x")
    return flags, z, effective_level


def _latest_qc(session: Session, analyte_code: str) -> LabQcRun | None:
    """The latest QC run that may decide the report-level gate for an
    analyte. Lab Depth W4: establishment runs stamped to a control
    lot that is not yet LIVE (or from before it went live) never
    decide the gate — see app.lab_reagents.qc_run_counts_for_gating.
    Runs with no control-lot stamp (every pre-W4 run) count exactly
    as before, so the Wave A gate is unchanged for them."""
    rows = session.scalars(
        select(LabQcRun).where(LabQcRun.analyte_code == analyte_code)
        .order_by(LabQcRun.id.desc()).limit(50)).all()
    if not any(r.control_lot_id is not None for r in rows):
        return rows[0] if rows else None
    from . import lab_reagents  # lazy: lab_reagents imports lab lazily too
    for run in rows:
        lot = lab_reagents.control_lot_by_id(session, run.control_lot_id)
        if lab_reagents.qc_run_counts_for_gating(run, lot):
            return run
    return None


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


def _duplicate_orders(session: Session, patient_ref: str, code: str,
                      exclude_id: str | None = None) -> list[dict]:
    """Other lab orders for the same patient + catalog code inside
    the stewardship window whose work is not finished (not cancelled
    / rejected) — the duplicate-order signal. Window + switch are the
    published catalog's `stewardship` config; with the check disabled
    or no window configured this returns nothing."""
    stewardship = _catalog(session).get("stewardship") or {}
    if stewardship.get("duplicateCheckEnabled") is not True:
        return []
    window = stewardship.get("duplicateWindowHours")
    if not isinstance(window, (int, float)) or isinstance(window, bool) \
            or window <= 0:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window))
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "ServiceRequest",
            FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.created_at.asc())).all()
    duplicates = []
    for row in rows:
        if row.resource_id == exclude_id:
            continue
        if _code_of(row.body or {}) != code:
            continue
        status = (row.body or {}).get("status")
        if status in ("cancelled", "rejected"):
            continue
        created = row.created_at
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                continue
        duplicates.append({"order_id": row.resource_id, "status": status,
                           "authored_on": (row.body or {}).get("authoredOn")})
    return duplicates


def _directory_violations(concept: dict, declared: dict) -> list[str]:
    """Named specimen-requirement violations for ONE concept (Lab
    Depth D1). Only SET requirements in the concept's published
    `directory.specimen` enforce, and only against specimen
    attributes the order DECLARES (body.specimen: container,
    specimenType / specimen_type, volumeMl / volume_ml, fasting):
    a declared value that contradicts a set requirement is a
    violation; an undeclared attribute is not judged here (the
    collection/specimen surfaces own it downstream)."""
    directory = concept.get("directory") or {}
    required = directory.get("specimen") or {}
    if not isinstance(required, dict) or not required:
        return []
    label = f"{concept.get('code')} ({concept.get('display')})"
    violations = []
    req_container = required.get("container")
    got_container = declared.get("container")
    if isinstance(req_container, str) and req_container.strip() \
            and isinstance(got_container, str) and got_container.strip() \
            and got_container.strip().lower() != req_container.strip().lower():
        violations.append(
            f"specimen container: {label} requires "
            f"'{req_container}' but the order declares '{got_container}'")
    req_type = required.get("specimenType")
    got_type = declared.get("specimenType") or declared.get("specimen_type")
    if isinstance(req_type, str) and req_type.strip() \
            and isinstance(got_type, str) and got_type.strip() \
            and got_type.strip().lower() != req_type.strip().lower():
        violations.append(
            f"specimen type: {label} requires '{req_type}' but the "
            f"order declares '{got_type}'")
    req_volume = required.get("minimumVolumeMl")
    got_volume = declared.get("volumeMl")
    if got_volume is None:
        got_volume = declared.get("volume_ml")
    if isinstance(req_volume, (int, float)) \
            and not isinstance(req_volume, bool) \
            and isinstance(got_volume, (int, float)) \
            and not isinstance(got_volume, bool) \
            and float(got_volume) < float(req_volume):
        violations.append(
            f"specimen volume: {label} requires a minimum of "
            f"{req_volume} mL but the order declares {got_volume} mL")
    if required.get("fastingRequired") is True \
            and declared.get("fasting") is False:
        violations.append(
            f"fasting: {label} requires a fasting specimen but the "
            "order declares fasting=false")
    return violations


def _enforce_directory_requirements(session: Session, concept: dict,
                                    body: dict) -> None:
    """Order-placement enforcement of the published directory's
    specimen requirements (Lab Depth D1): the ordered concept AND,
    for a panel, every component concept. Any violation refuses the
    order whole (422, every violation named) BEFORE any resource is
    written — the refusal discipline of the rest of this module."""
    declared = body.get("specimen")
    if not isinstance(declared, dict) or not declared:
        return
    concepts = [concept]
    if concept.get("panel"):
        for component_code in concept.get("components") or []:
            component = _concept(session, component_code)
            if component is not None:
                concepts.append(component)
    violations: list[str] = []
    for candidate in concepts:
        violations.extend(_directory_violations(candidate, declared))
    if violations:
        raise HTTPException(
            status_code=422,
            detail=("Order refused — published lab directory specimen "
                    "requirements not met: " + "; ".join(violations)))


def _order_warnings(session: Session, patient_ref: str, code: str,
                    new_order_id: str) -> list[dict]:
    """Stewardship warnings for a just-placed order (Depth pass).
    A warning NEVER blocks placement — the Phase 5 CPOE pattern."""
    concept = _concept(session, code) or {}
    warnings = []
    for dup in _duplicate_orders(session, patient_ref, code,
                                 exclude_id=new_order_id):
        warnings.append({
            "type": "duplicate-order",
            "code": code,
            "message": (f"Possible duplicate: another "
                        f"{concept.get('display') or code} order "
                        f"({dup['order_id']}) for this patient is "
                        f"'{dup['status']}' inside the stewardship "
                        "window. Review before collecting a second "
                        "specimen."),
            "existing_order_id": dup["order_id"],
            "existing_status": dup["status"],
        })
    return warnings


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
    # Lab Depth D1: the published directory's specimen requirements
    # are enforced here, before the order exists — a violating order
    # refuses whole and writes nothing.
    _enforce_directory_requirements(session, concept, body)

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
    rendered = fhir.create_resource("ServiceRequest", order_body, session, user)
    # Stewardship (Depth pass): a duplicate inside the published
    # window rides back as a warning on the created order — the
    # order stands; the human decides.
    warnings = _order_warnings(session, subject_ref, code, rendered["id"])
    if warnings:
        rendered = {**rendered, "warnings": warnings}
    return rendered


@router.get("/orders")
def list_orders(status: str | None = Query(default=None),
                patient: str | None = Query(default=None),
                bench: str | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("lab.read"))):
    stmt = select(FhirResource).where(FhirResource.resource_type == "ServiceRequest")
    if status:
        stmt = stmt.where(FhirResource.status == status)
    if patient:
        stmt = stmt.where(FhirResource.patient_ref == fhir._normalize_patient_ref(patient))
    rows = session.scalars(stmt.order_by(FhirResource.created_at.asc(),
                                         FhirResource.id.asc())).all()
    if bench:
        # Bench worklists (Depth pass): the core benches share the
        # standard spine, so their worklist is this same queue
        # filtered by the catalog bench of each order's code.
        rows = [row for row in rows
                if _order_bench(session, row) == bench]
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


# ---------------------------------------------------------------------------
# Test detail (UX pass, WS3a): one composed drill-down for a single
# order — step timeline, specimen, aliquot lineage, result detail,
# report + acknowledgement trail, custody. Composed from existing
# stores only (FHIR rows, the plate tables, the specimen lineage
# tables, the notification record and the audit chain); nothing is
# invented — a step that has not happened is null / pending, a
# lineage that does not exist is an empty list.
# ---------------------------------------------------------------------------

def _audit_latest(session: Session, entity: str, action: str,
                  ) -> AuditLog | None:
    return session.scalars(
        select(AuditLog).where(AuditLog.entity == entity,
                               AuditLog.action == action)
        .order_by(AuditLog.id.desc())).first()


def _audit_transition(session: Session, order_id: str, to_status: str,
                      ) -> AuditLog | None:
    rows = session.scalars(
        select(AuditLog).where(
            AuditLog.entity == f"ServiceRequest/{order_id}",
            AuditLog.action == "lab.order.transition")
        .order_by(AuditLog.id.desc())).all()
    for row in rows:
        if (row.payload or {}).get("to_status") == to_status:
            return row
    return None


def _delta_flag_excluding(session: Session, patient_ref: str | None,
                          concept: dict, current_value: float,
                          exclude_ids: set[str]) -> dict | None:
    """_delta_flag recomputed for the test-detail view, excluding the
    report's OWN observations from the baseline scan. The persisted
    Observation body carries no delta flag (result entry computes it
    transiently for the verification queue), so the detail view
    recomputes it — and once the report under view has been
    RELEASED, its own observations are final + untagged and would
    otherwise become their own baseline (change 0, never a flag)."""
    delta_cfg = concept.get("delta")
    if not isinstance(delta_cfg, dict) or not patient_ref:
        return None
    previous, previous_at = None, None
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Observation",
            FhirResource.patient_ref == patient_ref,
            FhirResource.status == "final")
        .order_by(FhirResource.id.desc())).all()
    for row in rows:
        if row.resource_id in exclude_ids:
            continue
        if fhir.has_unreleased_tag(row.body):
            continue
        if _code_of(row.body or {}) != concept["code"]:
            continue
        value = (row.body or {}).get("valueQuantity", {}).get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            previous = float(value)
            previous_at = _parse_ts((row.body or {}).get("effectiveDateTime"))
            break
    if previous is None:
        return None
    window = delta_cfg.get("windowHours")
    if isinstance(window, (int, float)) and not isinstance(window, bool) \
            and previous_at is not None:
        age_hours = (datetime.now(timezone.utc)
                     - previous_at).total_seconds() / 3600.0
        if age_hours > float(window):
            return None
    change = current_value - previous
    change_pct = (abs(change) / abs(previous) * 100.0) if previous else None
    exceeded = False
    if isinstance(delta_cfg.get("absolute"), (int, float)) \
            and abs(change) > delta_cfg["absolute"]:
        exceeded = True
    if isinstance(delta_cfg.get("pct"), (int, float)) \
            and change_pct is not None and change_pct > delta_cfg["pct"]:
        exceeded = True
    if not exceeded:
        return None
    return {"code": concept["code"], "previous_value": previous,
            "current_value": current_value, "change": change,
            "change_pct": change_pct,
            "previous_at": previous_at.isoformat() if previous_at else None,
            "window_hours": delta_cfg.get("windowHours")}


@router.get("/orders/{order_id}/test-detail")
def test_detail(order_id: str, session: Session = Depends(get_db),
                user: User = Depends(require_capability("lab.read"))):
    """The per-test drill-down: where this test is in its journey
    (steps), what specimen / aliquots it ran on, its results with
    ranges + interpretation + delta + critical flags, the report's
    verification / release summary, and the critical-notification
    acknowledgement trail."""
    order = _order_row(session, order_id)
    order_body = order.body or {}
    specimen = _specimen_row(session, order)
    custody = _custody(order, specimen)
    order_status = order_body.get("status")

    # The order's reports (the order_detail basedOn scan); the
    # detail view reads the LATEST one.
    report_rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "DiagnosticReport")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    report = None
    for candidate in report_rows:
        refs = [(ref or {}).get("reference")
                for ref in (candidate.body or {}).get("basedOn") or []]
        if f"ServiceRequest/{order_id}" in refs:
            report = candidate

    # Plate assignment (virtual plates, app.lab_plates) — the
    # 'plated' step exists ONLY when an assignment exists.
    plate_well = None
    if specimen is not None:
        from .lab_plates import plate_assignment
        plate_well = plate_assignment(session, specimen.resource_id)

    # Audit-derived actors / times for the governed steps.
    collected_audit = _audit_transition(session, order_id, "collected")
    received_audit = _audit_transition(session, order_id, "received")
    in_process_audit = _audit_transition(session, order_id, "in-process")
    results_audit = verify_audit = release_audit = None
    if report is not None:
        entity = f"DiagnosticReport/{report.resource_id}"
        results_audit = _audit_latest(session, entity, "lab.results.create")
        verify_audit = _audit_latest(session, entity, "lab.report.verify")
        release_audit = _audit_latest(session, entity, "lab.report.release")

    def _at(audit_row):
        return audit_row.created_at.isoformat() \
            if audit_row is not None and audit_row.created_at else None

    # Step states: 'done' from real timestamps / statuses; the first
    # not-done step is 'current'; later steps are 'pending'. The
    # final 'resulted' step is the RELEASED result (report issued) —
    # it follows verification in the journey, per the contract order.
    raw_steps = [
        ("ordered", "Ordered", True, order_body.get("authoredOn"),
         (order_body.get("requester") or {}).get("display"), {}),
        ("collected", "Collected", custody["collected_at"] is not None,
         custody["collected_at"],
         collected_audit.actor if collected_audit else None, {}),
        ("received", "Received", custody["received_at"] is not None,
         custody["received_at"],
         received_audit.actor if received_audit else None, {}),
    ]
    if plate_well is not None:
        raw_steps.append((
            "plated", "Plated", True,
            plate_well.assigned_at.isoformat()
            if plate_well.assigned_at else None,
            plate_well.assigned_by, {"plate_id": plate_well.plate_id}))
    analysed = report is not None
    raw_steps += [
        ("in_analysis", "In analysis",
         custody["in_analysis_at"] is not None
         or order_status in ("in-process", "resulted", "verified",
                             "released"),
         custody["in_analysis_at"],
         in_process_audit.actor if in_process_audit else None, {}),
        ("analysed", "Analysed", analysed,
         (report.body or {}).get("effectiveDateTime") if report else None,
         results_audit.actor if results_audit else None, {}),
        ("verified", "Verified",
         order_status in ("verified", "released")
         or verify_audit is not None,
         _at(verify_audit),
         verify_audit.actor if verify_audit else None, {}),
        ("resulted", "Resulted", order_status == "released",
         (report.body or {}).get("issued") if report else None,
         release_audit.actor if release_audit else None, {}),
    ]
    steps = []
    seen_open = False
    for key, label, done, at, by, extra in raw_steps:
        if done:
            state = "done"
        elif not seen_open:
            state = "current"
            seen_open = True
        else:
            state = "pending"
        if not done and state == "current":
            seen_open = True
        step = {"key": key, "label": label, "state": state,
                "at": at if done else None, "by": by if done else None}
        step.update(extra)
        steps.append(step)

    # Specimen summary + aliquot lineage (app.lab_specimens nodes;
    # empty honestly when this specimen has no lineage rows).
    specimen_out = None
    aliquots: list[dict] = []
    if specimen is not None:
        sbody = specimen.body or {}
        specimen_out = {
            "id": specimen.resource_id, "status": sbody.get("status"),
            "accession": (sbody.get("accessionIdentifier") or {}).get("value"),
            "type": (sbody.get("type") or {}).get("text"),
            "subject_ref": (sbody.get("subject") or {}).get("reference"),
            "collected_at": custody["collected_at"],
            "received_at": custody["received_at"],
            "in_analysis_at": custody["in_analysis_at"],
        }
        from .lab_specimens import LabSpecimenNode, _node_out
        node = session.scalars(
            select(LabSpecimenNode).where(
                LabSpecimenNode.fhir_specimen_id == specimen.resource_id)
        ).first()
        if node is not None:
            members = session.scalars(
                select(LabSpecimenNode).where(
                    LabSpecimenNode.root_key == node.root_key,
                    LabSpecimenNode.derivation_type != "primary")
                .order_by(LabSpecimenNode.id.asc())).all()
            aliquots = [_node_out(session, m) for m in members]

    # Results detail from the report's observations.
    results_out: list[dict] = []
    report_out = None
    acknowledgements: list[dict] = []
    if report is not None:
        observations = _result_observations(session, report)
        obs_ids = {o.resource_id for o in observations}
        patient_ref = (report.body or {}).get("subject", {}).get("reference")
        for obs in observations:
            body = obs.body or {}
            code = _code_of(body)
            concept = _concept(session, code) if code else None
            quantity = body.get("valueQuantity") or {}
            value = quantity.get("value")
            if value is None:
                value = body.get("valueString")
            interp = (((body.get("interpretation") or [{}])[0]
                       .get("coding") or [{}])[0].get("code"))
            rr_list = body.get("referenceRange") or []
            rr0 = rr_list[0] if rr_list and isinstance(rr_list[0], dict) \
                else {}
            ref_range = None
            if rr0:
                ref_range = {
                    "low": (rr0.get("low") or {}).get("value"),
                    "high": (rr0.get("high") or {}).get("value"),
                    "unit": quantity.get("unit")
                    or (rr0.get("low") or {}).get("unit")
                    or (rr0.get("high") or {}).get("unit")}
            elif concept is not None:
                rng = concept.get("referenceRange") or {}
                if rng.get("low") is not None or rng.get("high") is not None:
                    ref_range = {"low": rng.get("low"),
                                 "high": rng.get("high"),
                                 "unit": rng.get("unit")
                                 or concept.get("unit")}
            delta = None
            if isinstance(value, (int, float)) \
                    and not isinstance(value, bool) and concept is not None:
                delta = _delta_flag_excluding(
                    session, patient_ref, concept, float(value), obs_ids)
            results_out.append({
                "analyte": code, "code": code,
                "display": (body.get("code") or {}).get("text"),
                "observation_id": obs.resource_id,
                "value": value, "unit": quantity.get("unit"),
                "reference_range": ref_range,
                "interpretation": interp,
                "interpretation_text": ((body.get("interpretation")
                                         or [{}])[0].get("text")),
                "delta": delta, "critical": _is_critical(interp),
                "status": body.get("status"),
            })
        report_out = {
            "report_id": report.resource_id,
            "status": (report.body or {}).get("status"),
            "verified_by": verify_audit.actor if verify_audit else None,
            "verified_at": _at(verify_audit),
            "released_by": release_audit.actor if release_audit else None,
            "released_at": (report.body or {}).get("issued")
            or _at(release_audit),
            "effective_at": (report.body or {}).get("effectiveDateTime"),
            "critical": _report_is_critical(session, report),
        }
        record = _notification_record(session, report.resource_id)
        if record is not None:
            acknowledgements.append({
                "report_id": report.resource_id,
                **_notification_out(session, report,
                                    _report_is_critical(session, report)),
            })

    code = _code_of(order_body)
    return {
        "order": {
            "id": order.resource_id, "status": order_status,
            "code": code,
            "display": (order_body.get("code") or {}).get("text"),
            "patient_ref": (order_body.get("subject") or {}).get("reference"),
            "authored_on": order_body.get("authoredOn"),
            "requester": (order_body.get("requester") or {}).get("display"),
            "priority": order_body.get("priority"),
        },
        "steps": steps,
        "specimen": specimen_out,
        "aliquots": aliquots,
        "results": results_out,
        "report": report_out,
        "acknowledgements": acknowledgements,
        "custody": custody,
    }


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
    keeps the original numeric-only rule, byte for byte.

    Lab Depth D6: a result item may carry analyser `flags` (a list of
    flag-code strings — OBX-8 on HL7, the CSV flags column, or a
    direct-entry list). Flags are first-class data: preserved on the
    checked entry, stamped onto the Observation, and consumed by
    autoverification (D7) and the rerun queue (app.lab_analyser)."""
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
        # Lab Depth D6: analyser flags ride the checked entry (a list
        # of flag-code strings; anything else is a 422, never coerced).
        raw_flags = item.get("flags")
        if raw_flags is None:
            flags: list[str] = []
        elif isinstance(raw_flags, list) and all(
                isinstance(f, str) and f.strip() for f in raw_flags):
            flags = [f.strip() for f in raw_flags]
        else:
            raise HTTPException(
                status_code=422,
                detail=f"results[{i}].flags must be a list of flag-code "
                       "strings (analyser flags, e.g. OBX-8)")
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
                            "unit": None, "qualitative": True,
                            "flags": flags})
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422,
                                detail=f"results[{i}].value must be a number")
        unit = item.get("unit") or analyte.get("unit") or \
            (analyte.get("referenceRange") or {}).get("unit")
        checked.append({"concept": analyte, "value": float(value),
                        "unit": unit, "qualitative": False,
                        "flags": flags})
    return checked


# ---------------------------------------------------------------------------
# Post-analytical (Phase 6B): delta checks, reflex rules, auto-verification
# ---------------------------------------------------------------------------

def _parse_ts(raw) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else \
        parsed.replace(tzinfo=timezone.utc)


def _previous_released(session: Session, patient_ref: str | None,
                       code: str) -> tuple[float | None, datetime | None]:
    """(value, effective time) of the patient's most recent RELEASED
    (final, untagged) numeric result for an analyte — the delta-check
    baseline. Drafts never count: they are preliminary +
    visibility-tagged by construction."""
    if not patient_ref:
        return None, None
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
            return float(value), _parse_ts(
                (row.body or {}).get("effectiveDateTime"))
    return None, None


def _previous_released_value(session: Session, patient_ref: str | None,
                             code: str) -> float | None:
    value, _at = _previous_released(session, patient_ref, code)
    return value


def _delta_flag(session: Session, patient_ref: str | None,
                concept: dict, current_value: float) -> dict | None:
    """A delta-check flag for one numeric result, or None. Thresholds
    are the analyte's catalog `delta` config ({absolute} or {pct});
    analytes without delta config never flag. A flag is information
    for the verifying clinician — it never blocks anything.

    Depth pass: the config may also carry `windowHours` — the
    comparison window real delta-check programmes run with (a
    creatinine is compared against the last 72 hours, not last
    year's). A baseline older than the window is no baseline: no
    flag. Without `windowHours` the most recent released value is
    the baseline however old (prior behaviour, unchanged)."""
    delta_cfg = concept.get("delta")
    if not isinstance(delta_cfg, dict):
        return None
    previous, previous_at = _previous_released(session, patient_ref,
                                               concept["code"])
    if previous is None:
        return None
    window = delta_cfg.get("windowHours")
    if isinstance(window, (int, float)) and not isinstance(window, bool) \
            and previous_at is not None:
        age_hours = (datetime.now(timezone.utc)
                     - previous_at).total_seconds() / 3600.0
        if age_hours > float(window):
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
            "change_pct": change_pct,
            "previous_at": previous_at.isoformat() if previous_at else None,
            "window_hours": delta_cfg.get("windowHours")}


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
    """Reflex testing (Phase 6B; depth LAB-D11): for each created
    result matching a published reflex rule, the add-on runs against
    the source order's EXISTING specimen — the implementation lives
    in app.lab_tat.fire_reflex_same_specimen (same-specimen volume
    check + consumption through app.lab_specimens, named refusals,
    cascade evidence, reflex-billing flag). Deduped per (report,
    rule): re-entry never double-creates. Returns the created
    add-on order ids, as before."""
    from . import lab_tat
    return lab_tat.fire_reflex_same_specimen(session, user, order,
                                              report_id, checked)


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
    demographics = _demographics_for_order(session, order)
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
        if _is_critical(interpret_value(value, entry["concept"],
                                        demographics)):
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
                      results, source: str,
                      device_key: str | None = None,
                      reagent_lot: dict | None = None,
                      instrument_id: str | None = None) -> dict:
    """Create preliminary, unreleased-tagged Observations + DiagnosticReport
    for an order in received/in-process state, and move order + specimen to
    resulted. Used identically by manual entry and analyser ingest: ingest
    results are drafts in exactly the same way (no auto-release).

    Lab Depth W4 (LAB-D8/D9): when the entry names a reagent lot
    and/or an instrument, two further gates run BEFORE any write
    (a refusal leaves zero writes): the reagent-lot stage/expiry/
    verification lockout (app.lab_reagents.enforce_reagent_lot — no
    emergency bypass) and the instrument-path lock (app.lab_qc.
    enforce_instrument_path — failed latest QC or overdue/failed
    calibration for that instrument). Created Observations (and the
    report) are then STAMPED with the reagent lot + instrument so a
    lot's results can be enumerated (lot -> result traceability)."""
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
    # Lab Depth W4 gates (LAB-D8/D9), before ANY write: reagent-lot
    # lockout + instrument-path lock. Lazy imports: app.lab_reagents
    # and app.lab_qc import app.lab lazily for the same cycle reason
    # as app.lab_plates below.
    effective_instrument = instrument_id or device_key
    if reagent_lot is not None:
        from . import lab_reagents
        lab_reagents.enforce_reagent_lot(session, reagent_lot)
    if effective_instrument is not None:
        from . import lab_qc
        lab_qc.enforce_instrument_path(
            session, effective_instrument,
            [c["concept"]["code"] for c in checked])
    # Lab Depth D6(a) / LAB-m2 gate, before ANY write, on EVERY path
    # that reaches this core (manual/direct entry, HL7 + CSV ingest,
    # the MLLP listener via ingest_hl7_groups, send-outs, plates,
    # rerun submission): an instrument whose EFFECTIVE registry
    # status (app.lab_analyser's reconciliation across the lab and
    # device registries) is in-maintenance or out-of-service has its
    # batch QUARANTINED by name and refused 409 — zero results are
    # written. The quarantine row is committed by the gate itself so
    # the refusal record survives this refusal.
    if effective_instrument is not None:
        lab_analyser.enforce_ingest_gate(
            session, effective_instrument, source_format=source,
            actor=user.username,
            payload={"kind": "results", "order_id": order.resource_id,
                     "patient_ref": (order.body or {}).get(
                         "subject", {}).get("reference"),
                     "results": results if isinstance(results, list)
                     else []})
    # Lab Depth W9 (LAB-D16b) competency gate, before ANY write,
    # composed with the W4 lockouts above and never weakening them:
    # for catalogue tests MAPPED in the published competency gating
    # map (terminology/lab-competency-gating), the entering actor
    # must hold a CURRENT CLIA six-element competency for the
    # required test system — expired / incomplete / absent refuses
    # 409 BY NAME. Unmapped tests are ungated (the seeded map is
    # empty; the QMS gating surface lists every test's state).
    from . import lab_poct_qms as _lab_qms
    _lab_qms.enforce_competency_gate(
        session, user, [c["concept"]["code"] for c in checked],
        stage="result entry")
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
        # POCT programme enforcement (Depth pass): the published
        # `terminology/poct-programme` switches, BOTH seeded off.
        # When a site publishes one on, entry refuses here — before
        # any write — until the operator holds a current competency
        # for the ordered test and/or the named device has passing
        # QC at every required level today. Lab Depth W9 (LAB-D15):
        # the governed deployment profile (terminology/
        # poct-deployment-profile) is ORed over the programme
        # switches by _poct_effective_enforcement — in the profile's
        # "enforce" mode its block flags enforce even while the
        # programme's own switches remain off; in "track-only" mode
        # this block behaves exactly as before W9.
        programme = _poct_programme(session)
        enforcement = _poct_effective_enforcement(session, programme)
        if enforcement.get("blockUncompetentOperator") is True \
                and not _competency_current(
                    session, programme, _operator_ids(session, user),
                    order_concept_pre["code"]):
            raise HTTPException(
                status_code=409,
                detail=(f"POCT programme: operator '{user.username}' holds "
                        f"no current competency record for "
                        f"'{order_concept_pre['code']}' "
                        f"({order_concept_pre.get('display')}); record the "
                        "assessment in the POCT programme before entering "
                        "results"))
        if enforcement.get("blockMissingDailyQc") is True:
            if not device_key:
                raise HTTPException(
                    status_code=422,
                    detail=("POCT programme: 'device_key' is required — "
                            "daily-QC enforcement is on, so the result "
                            "must name the device that produced it"))
            if not _poct_qc_current(session, programme, device_key):
                raise HTTPException(
                    status_code=409,
                    detail=(f"POCT programme: device '{device_key}' has no "
                            "passing QC at every required level today; run "
                            "the controls and record them in the POCT "
                            "programme before entering results"))
    order_concept = _order_code(session, order)
    subject_ref = (order.body or {}).get("subject", {}).get("reference")
    demographics = _demographics_for_order(session, order)
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
            interp_code = interpret_value(value, analyte, demographics)
            if _is_critical(interp_code):
                any_critical = True
            obs_body["valueQuantity"] = {"value": value, "unit": unit}
            if interp_code:
                obs_body["interpretation"] = [{
                    "coding": [{"system": INTERPRETATION_SYSTEM, "code": interp_code,
                                "display": _INTERPRETATION_TEXT[interp_code]}],
                    "text": _INTERPRETATION_TEXT[interp_code]}]
            # The range recorded on the observation is the EFFECTIVE
            # range (Lab Depth D1): the age/sex partition that governed
            # interpretation when one matched, else the default.
            rng = select_reference_range(analyte, demographics)
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
        # Lab Depth W4 (LAB-D9): lot -> result traceability stamp —
        # the reagent lot + instrument that produced this result,
        # as Observation extensions (the enumeration surface is
        # GET /lab/reagent-lots/results in app.lab_reagents).
        obs_extensions = []
        if reagent_lot is not None:
            from . import lab_reagents as _lr
            obs_extensions.append({
                "url": _lr.REAGENT_LOT_EXTENSION,
                "valueString": _lr.lot_key_for(
                    str(reagent_lot.get("sku", "")).strip(),
                    str(reagent_lot.get("location_code", "")).strip(),
                    str(reagent_lot.get("lot_number", "")).strip())})
        if effective_instrument is not None:
            obs_extensions.append({"url": INSTRUMENT_EXTENSION,
                                   "valueString": effective_instrument})
        # Lab Depth D6(b): analyser flags are first-class data on the
        # result — one extension per flag, read by D7 autoverification
        # traces (via the checked entries) and by staff on the result.
        for flag in entry.get("flags") or []:
            obs_extensions.append({"url": lab_analyser.FLAG_EXTENSION,
                                   "valueString": flag})
        if obs_extensions:
            obs_body["extension"] = obs_extensions
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
    if device_key or instrument_id or reagent_lot is not None:
        # The device/instrument that produced the results (POCT
        # programme + LAB-D8 keying): the same instrument extension
        # the analyser-ingest path writes. LAB-D9 adds the reagent
        # lot stamp at report level too (observations carry it as
        # the per-result record).
        exts = list(report_body.get("extension") or [])
        if effective_instrument is not None and not any(
                e.get("url") == INSTRUMENT_EXTENSION for e in exts
                if isinstance(e, dict)):
            exts.append({"url": INSTRUMENT_EXTENSION,
                         "valueString": effective_instrument})
        if reagent_lot is not None:
            from . import lab_reagents as _lr
            exts.append({
                "url": _lr.REAGENT_LOT_EXTENSION,
                "valueString": _lr.lot_key_for(
                    str(reagent_lot.get("sku", "")).strip(),
                    str(reagent_lot.get("location_code", "")).strip(),
                    str(reagent_lot.get("lot_number", "")).strip())})
        report_body["extension"] = exts
    report = fhir.create_resource("DiagnosticReport", report_body, session, user)

    _set_status(order, "resulted")
    if specimen is not None:
        _set_status(specimen, "resulted")
    session.flush()
    append_audit(session, actor=user.username, action="lab.results.create",
                 entity=f"DiagnosticReport/{report['id']}",
                 payload={"order_id": order.resource_id, "source": source,
                          "result_count": len(observations),
                          "critical": any_critical,
                          # LAB-D8/D9 stamps (traceability audit trail).
                          "reagent_lot": reagent_lot,
                          "instrument_id": effective_instrument})
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
    # Lab Depth D6(c): results carrying a governed rerun flag route to
    # the rerun queue (app.lab_analyser) — their rerun output will
    # re-enter THIS core, never a side door.
    rerun_items = lab_analyser.register_rerun_items(
        session, order_id=order.resource_id, report_id=report["id"],
        checked=checked, instrument_id=effective_instrument,
        actor=user.username)
    if rerun_items:
        session.commit()
    auto_verified = False
    auto_released = False
    autoverify_evaluation = None
    if not poct:
        # Lab Depth D7: when the order's bench has a PUBLISHED, enabled
        # autoverification rule set (terminology/lab-autoverify), the
        # ONE evaluation function in app.lab_autoverify judges the
        # set: a full pass auto-RELEASES (verify + release, traced);
        # any hold is recorded with its rule trace and the draft stays
        # for the manual path. Benches without a live set fall through
        # to the Phase 6B verify-only path below, unchanged.
        evaluation = lab_autoverify.evaluate_autoverification(
            session, order, checked, instrument_id=effective_instrument)
        if evaluation["applicable"]:
            autoverify_evaluation = {
                "bench": evaluation["bench"],
                "rule_set_version": evaluation["rule_set_version"],
                "decision": evaluation["decision"],
                "trace": evaluation["trace"],
                "hold_reasons": evaluation["hold_reasons"]}
            report_row = fhir._get_row("DiagnosticReport", report["id"],
                                       session)
            if evaluation["decision"] == "release":
                auto_released = lab_autoverify.release_and_record(
                    session, report_row, order, evaluation,
                    instrument_id=effective_instrument)
                auto_verified = auto_released
                if auto_released:
                    report = fhir._render(report_row)
                    observations = [
                        fhir._render(fhir._get_row(
                            "Observation", o["id"], session))
                        for o in observations]
            else:
                lab_autoverify.record_hold(
                    session, report_row, order, evaluation,
                    instrument_id=effective_instrument)
        else:
            applies, rule_ids = _auto_verify_decision(session, order, checked)
            if applies:
                report_row = fhir._get_row("DiagnosticReport", report["id"], session)
                auto_verified = _auto_verify(session, report_row, order,
                                             checked, rule_ids)
                if auto_verified:
                    report = fhir._render(report_row)
    return {"report": report, "observations": observations,
            "critical": any_critical, "reflex_orders": reflex_orders,
            "rerun_items": rerun_items,
            "auto_verified": auto_verified,
            "auto_released": auto_released,
            "autoverify": autoverify_evaluation}


@router.post("/orders/{order_id}/results", status_code=201)
async def enter_results(order_id: str, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    order = _order_row(session, order_id)
    # Virtual-plate QC gate (UX pass, app.lab_plates): when this
    # order's specimen sits on an OPEN plate whose controls have not
    # all passed, direct result entry is refused — the plate's gate
    # governs every entry path for that specimen. Non-plated orders
    # and completed plates are unaffected. The check runs before any
    # result is written. (Lazy import: app.lab_plates imports
    # app.lab for the shared result path.)
    specimen = _specimen_row(session, order)
    if specimen is not None:
        from .lab_plates import plate_gate_blocker
        blocker_plate = plate_gate_blocker(session, specimen.resource_id)
        if blocker_plate is not None:
            raise HTTPException(
                status_code=409,
                detail=(f"Specimen {specimen.resource_id} is assigned to "
                        f"plate {blocker_plate}, whose QC gate is closed: "
                        "plate QC gate: controls not yet passed"))
    return _create_results(session, user, order, payload.get("results"),
                            source="manual",
                            device_key=payload.get("device_key"),
                            # LAB-D8/D9: the reagent lot + instrument
                            # the entry names (either may be absent —
                            # unstamped legacy entry is unchanged).
                            reagent_lot=payload.get("reagent_lot"),
                            instrument_id=payload.get("instrument_id"))


# ---------------------------------------------------------------------------
# Analyser ingest: HL7 v2 ORU^R01 and CSV
# ---------------------------------------------------------------------------

def parse_hl7_oru(content: str) -> tuple[str, str | None, list[dict]]:
    """The HL7 ORU reader for the ingest paths (Lab Depth D6): the
    shared app.hl7 reader, PLUS the OBX-8 abnormal/analyser flags,
    which land on each result dict as `flags` — first-class data for
    autoverification (D7) and the rerun queue. Callers that imported
    lab.parse_hl7_oru before D6 (app.interop, app.devices,
    app.pipelines) get the enriched groups transparently; the tuple
    shape and every parse error are the base reader's, unchanged."""
    order_id, patient_id, results = _parse_hl7_oru_base(content)
    lines = [ln.strip() for ln in
             content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
             if ln.strip()]
    if not lines or not lines[0].startswith("MSH"):
        return order_id, patient_id, results
    sep = lines[0][3] if len(lines[0]) > 3 else "|"
    obx_flags: list[list[str]] = []
    for line in lines:
        parts = line.split(sep)
        if parts[0] == "OBX":
            raw = _hl7_field(parts, 8)
            obx_flags.append([f for f in
                              raw.replace("^", " ").replace(",", " ").split()
                              if f])
    for result, flags in zip(results, obx_flags):
        if flags:
            result["flags"] = flags
    return order_id, patient_id, results


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
    per group.

    Lab Depth D6(a) / LAB-m2: the batch is gated ONCE here, before
    any group is created — a batch from an in-maintenance or
    out-of-service instrument (effective status across both
    registries, app.lab_analyser) is quarantined whole under its
    full group payload (so disposition reprocessing can replay it)
    and refused 409; the per-result gate inside _create_results is
    the second, identical check for paths that bypass this core."""
    if instrument_id is not None:
        lab_analyser.enforce_ingest_gate(
            session, instrument_id, source_format=source_format,
            actor=user.username,
            payload={"kind": "groups",
                     "groups": [[oid, pid, res] for oid, pid, res in groups]})
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
        # LAB-D8: ingest runs through the same instrument-path lock
        # as manual entry (inside _create_results) — a QC-failed or
        # calibration-locked instrument's results are refused here
        # before any write, not merely gated at report release.
        outcome = _create_results(session, user, order, results,
                                  source=f"ingest-{source_format}",
                                  instrument_id=instrument_id)
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
    order_id, code, value and optional patient_id, unit, flags. The
    optional `flags` cell carries analyser flag codes separated by
    ';' (Lab Depth D6 — first-class flag data, like OBX-8 on HL7).
    Rows are grouped by order. Returns [(order_id, patient_id,
    [results])]."""
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
        result_row = {"code": code, "value": value,
                      "unit": (row.get("unit") or "").strip() or None}
        flags = [f.strip() for f in
                 (row.get("flags") or "").replace(",", ";").split(";")
                 if f.strip()]
        if flags:
            result_row["flags"] = flags
        grouped[order_id][1].append(result_row)
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
    # and is recorded as metadata on each created report. Lab Depth
    # D6: "registered" reads the reconciled registry view
    # (app.lab_analyser) — an instrument known to EITHER the lab
    # registry or the Phase 14 device registry is registered; its
    # effective status then gates the batch in ingest_hl7_groups
    # (LAB-m2 quarantine on in-maintenance / out-of-service).
    instrument_id = payload.get("instrument_id")
    if instrument_id is not None:
        if not lab_analyser.instrument_known(session, str(instrument_id)):
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
    # Lab Depth W9 (LAB-D16b): the competency gate at VERIFICATION —
    # the verifying actor must hold current CLIA competency for
    # every mapped test system on this report (same governed map as
    # result entry; unmapped tests ungated). Composes with the QC
    # gate above; refuses 409 BY NAME before any state change.
    from . import lab_poct_qms as _lab_qms
    _lab_qms.enforce_competency_gate(
        session, user,
        [_code_of(o.body or {}) for o in _result_observations(session, report)],
        stage="verification")
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
    # Phase 10 (Track B): the release is also a rules-engine event. The
    # seeded critical-value-notification rule re-expresses the critical
    # escalation through the engine: on a critical release it OPENS the
    # notification record (pending) — the worklist item the notify /
    # acknowledge workflow above then completes. Lazy import for the
    # same cycle reason (app.rules imports app.fhir/models only, but
    # the hook keeps the one-way pattern obvious).
    from .rules import evaluate_rules
    evaluate_rules(session, "lab.report.released",
                   {"critical": _report_is_critical(session, report),
                    "report_id": report.resource_id,
                    "order_id": order.resource_id,
                    "patient_ref": (report.body or {}).get("subject", {})
                    .get("reference")},
                   actor=user.username)


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
    demographics = _demographics_for_order(session, order)
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
            interp_code = interpret_value(value, analyte, demographics)
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
    # Lab Depth W4 (LAB-D8/D9) keying: a run may name its instrument
    # and reagent lot, and a control material that is a registered
    # control LOT must be LIVE to be used as a patient-QC material
    # (named refusal, before any write). The shift is DERIVED from
    # the governed shift windows at the run's UTC timestamp — never
    # caller-asserted. Lazy imports (cycle: lab_reagents/lab_qc
    # import lab lazily).
    from . import lab_qc as _lab_qc
    from . import lab_reagents as _lab_reagents
    instrument_id = payload.get("instrument_id")
    if instrument_id is not None:
        instrument_id = _lab_qc._require_instrument(session, instrument_id)
    reagent_lot_key = None
    if isinstance(payload.get("reagent_lot_key"), str) \
            and payload["reagent_lot_key"].strip():
        reagent_lot_key = payload["reagent_lot_key"].strip()
    elif isinstance(payload.get("reagent_lot"), dict):
        _rl = payload["reagent_lot"]
        reagent_lot_key = _lab_reagents.lot_key_for(
            str(_rl.get("sku", "")).strip(),
            str(_rl.get("location_code", "")).strip(),
            str(_rl.get("lot_number", "")).strip())
    control_blocker = _lab_reagents.control_lot_use_blocker(session, material)
    if control_blocker:
        raise HTTPException(status_code=409, detail=control_blocker)
    control_lot = _lab_reagents.control_lot_for(session, material)
    # Westgard programme evaluation (Phase 6B; full rule set LAB-D8):
    # flags + z-score are computed here from the published programme
    # config over the instrument/lot-keyed series and stored on the
    # run; `passed` below keeps its original range meaning.
    flags, z_score, effective_level = _westgard_flags(
        session, str(code), material, level, numbers["measured_value"],
        instrument_id=instrument_id, reagent_lot_key=reagent_lot_key)
    run = LabQcRun(
        analyte_code=str(code), control_material=material,
        expected_low=numbers["expected_low"], expected_high=numbers["expected_high"],
        measured_value=numbers["measured_value"],
        unit=payload.get("unit") if isinstance(payload.get("unit"), str) else None,
        passed=numbers["expected_low"] <= numbers["measured_value"] <= numbers["expected_high"],
        performed_by=user.username,
        level=effective_level, flags=flags, z_score=z_score,
        instrument_id=instrument_id, reagent_lot_key=reagent_lot_key,
        control_lot_id=control_lot.id if control_lot is not None else None,
        shift=_lab_qc.shift_for_timestamp(
            _lab_qc.reagent_qc_config(session),
            datetime.now(timezone.utc)))
    session.add(run)
    session.flush()
    append_audit(session, actor=user.username, action="lab.qc.run",
                 entity=f"LabQcRun/{run.id}",
                 payload={"analyte_code": run.analyte_code,
                          "control_material": run.control_material,
                          "measured_value": run.measured_value,
                          "passed": run.passed,
                          "level": run.level, "flags": run.flags,
                          "z_score": run.z_score,
                          "instrument_id": run.instrument_id,
                          "reagent_lot_key": run.reagent_lot_key,
                          "control_lot_id": run.control_lot_id,
                          "shift": run.shift})
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


# ---------------------------------------------------------------------------
# Depth pass: post-analytical governance, result trends, stewardship
# ---------------------------------------------------------------------------

@router.get("/governance")
def lab_governance(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    """The post-analytical governance surface: the state of the
    published rules a laboratorian is accountable for — auto-verify
    (seeded OFF; the off state is reported as data, never implied),
    the per-analyte auto-verify ranges, the reflex rules, the delta
    checks, and the stewardship duplicate window. Editing stays in
    Studio (the config key + published version ride along); this is
    the operational read the lab manages by. Recent auto-verification
    activity is read back from the audit chain."""
    catalog_cfg = published_config(session, "terminology", LAB_CATALOG_KEY)
    if catalog_cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published lab catalog (terminology/{LAB_CATALOG_KEY}); "
                    "publish one in Studio before using the lab"))
    catalog = catalog_cfg.payload or {}
    concepts = [c for c in (catalog.get("concepts") or [])
                if isinstance(c, dict)]
    auto = catalog.get("autoVerify") or {}
    auto_rules = [{"code": c["code"], "display": c.get("display"),
                   "low": (c.get("autoVerify") or {}).get("low"),
                   "high": (c.get("autoVerify") or {}).get("high")}
                  for c in concepts
                  if isinstance(c.get("autoVerify"), dict)]
    recent = []
    audit_rows = session.scalars(
        select(AuditLog).where(AuditLog.action == "lab.report.verify")
        .order_by(AuditLog.id.desc()).limit(100)).all()
    for row in audit_rows:
        payload = row.payload or {}
        if payload.get("auto_verified") is True:
            recent.append({"report_id": row.entity.split("/", 1)[-1],
                           "order_id": payload.get("order_id"),
                           "rule_ids": payload.get("rule_ids") or [],
                           "at": row.created_at.isoformat()
                           if row.created_at else None})
        if len(recent) >= 10:
            break
    return {
        "config": {"kind": "terminology", "key": LAB_CATALOG_KEY,
                   "version": catalog_cfg.version,
                   "edit_in": "System Studio -> Terminology -> lab-catalog"},
        "auto_verify": {
            "enabled": auto.get("enabled") is True,
            "state_label": ("ENABLED — qualifying drafts verify "
                            "automatically (never release)"
                            if auto.get("enabled") is True else
                            "DISABLED — every draft awaits a human verify "
                            "(the seeded, recommended default)"),
            "rules": auto_rules,
            "recent_auto_verified": recent,
        },
        "reflex_rules": catalog.get("reflexRules") or [],
        "delta_checks": [{"code": c["code"], "display": c.get("display"),
                          "config": c.get("delta")}
                         for c in concepts
                         if isinstance(c.get("delta"), dict)],
        "stewardship": catalog.get("stewardship") or {},
    }


@router.get("/trends")
def lab_trends(patient_id: str = Query(...), code: str = Query(...),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability("lab.read"))):
    """A patient's RELEASED result series for one analyte, oldest
    first — the viral-load trend view (virology) and the general
    trend surface behind it. Drafts never appear: only final,
    untagged observations count, exactly the delta-check baseline
    rule. log10 rides along for viral loads (value > 0)."""
    concept = _concept(session, code)
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown lab code '{code}': not in the published "
                   "lab catalog")
    fhir._get_row("Patient", patient_id, session)  # 404 if unknown
    patient_ref = f"Patient/{patient_id}"
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Observation",
            FhirResource.patient_ref == patient_ref,
            FhirResource.status == "final")
        .order_by(FhirResource.id.asc())).all()
    points = []
    for row in rows:
        body = row.body or {}
        if fhir.has_unreleased_tag(body):
            continue
        if _code_of(body) != code:
            continue
        value = (body.get("valueQuantity") or {}).get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        points.append({
            "observation_id": row.resource_id,
            "value": float(value),
            "unit": (body.get("valueQuantity") or {}).get("unit"),
            "effective": body.get("effectiveDateTime"),
            "log10": (round(math.log10(value), 3) if value > 0 else None),
        })
    points.sort(key=lambda p: p["effective"] or "")
    return {"patient": patient_ref, "code": code,
            "display": concept.get("display"),
            "bench": concept.get("bench"),
            "points": points, "count": len(points)}


@router.get("/stewardship/duplicates")
def stewardship_duplicates(session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.read"))):
    """The stewardship worklist: patient + analyte pairs with more
    than one unfinished order inside the published duplicate
    window — the utilisation view behind the order-time warning."""
    catalog = _catalog(session)
    stewardship = catalog.get("stewardship") or {}
    window = stewardship.get("duplicateWindowHours")
    groups: dict[tuple[str, str], dict] = {}
    if stewardship.get("duplicateCheckEnabled") is True \
            and isinstance(window, (int, float)) \
            and not isinstance(window, bool) and window > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window))
        rows = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "ServiceRequest")
            .order_by(FhirResource.created_at.asc())).all()
        for row in rows:
            body = row.body or {}
            if body.get("status") in ("cancelled", "rejected"):
                continue
            created = row.created_at
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < cutoff:
                    continue
            patient_ref = (body.get("subject") or {}).get("reference")
            code = _code_of(body)
            if not patient_ref or not code:
                continue
            concept = _concept(session, code) or {}
            if concept.get("bench") in ("microbiology", "pathology"):
                continue  # bench orders are worked in their benches
            group = groups.setdefault(
                (patient_ref, code),
                {"patient": patient_ref, "code": code,
                 "display": concept.get("display"), "orders": []})
            group["orders"].append({"order_id": row.resource_id,
                                    "status": body.get("status"),
                                    "authored_on": body.get("authoredOn")})
    duplicates = [g for g in groups.values() if len(g["orders"]) > 1]
    return {"window_hours": window,
            "enabled": stewardship.get("duplicateCheckEnabled") is True,
            "groups": duplicates, "total": len(duplicates)}
