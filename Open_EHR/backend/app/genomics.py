"""Phase 16 (WS-F1, Scope G) — Genomics: reported variant data +
pharmacogenomic (PGx) checks.

NO VARIANT-CALLING PIPELINE EXISTS OR IS IMPLIED BY THIS MODULE.
There is no sequencer interface, no VCF/BAM/FASTQ ingestion, and no
code here that derives a variant from raw reads. Variants arrive as
REPORTED RESULTS — a laboratory (internal or external) has already
called, classified and signed them out; this module records the
reported result in the chart, evaluates published pharmacogenomic
rules against the recorded variants, and answers gene-cohort queries
for research. Anything upstream of a reported result is out of scope.

What lives here:

- **Variant recording** (`POST /genomics/variants`, capability
  `genomics.work`): typed validation (patient exists, gene, HGVS
  c. required / p. optional, zygosity, ACMG-style interpretation),
  then the variant is written as a FHIR Observation through the FHIR
  store (`app.fhir.create_resource`, so the write is validated,
  projected and audit-logged like every other Observation).

  FHIR shape (following the HL7 FHIR Genomics Reporting IG loosely —
  a profile conformance claim is NOT made): the Observation carries
  category `laboratory`, a panel code (LOINC 81247-9, the genetic
  variant panel marker), the patient as `subject`, the reporting lab
  as `performer` when supplied, and one `component` per reported
  fact — gene studied (LOINC 48018-6, value = gene symbol), DNA
  change c.HGVS (LOINC 48004-6, valueString), amino-acid change
  p.HGVS (LOINC 48005-3, valueString, when reported), allelic state
  / zygosity (LOINC 53034-5, value = het | hom | hemi as coded
  concepts LA6705-3 / LA6706-1 / LA6707-9) and variant interpretation
  (an OpenCare component code, value = the ACMG-style term). Reads
  parse the components back into a flat variant summary.

- **Pharmacogenomics** (`terminology/pharmacogenomics` published
  Studio config, seeded by :func:`ensure_genomics_defaults`): PGx
  rules are GOVERNED CONFIGURATION, never code — the same posture as
  the Phase 9/15 medication-monitoring hook
  (`terminology/medication-monitoring` in app.pharmacy, rule shape
  there {medicationCode, condition-ish requiredLabs, action}; here
  {gene, variant, phenotype, drugCode, action, message}). The seeded
  payload is EXAMPLE content (labelled as such in its description):
  realistic, synthetic rules a site MUST replace with its own
  governed PGx source before clinical use. Rule `variant` matching
  semantics (documented in the seed description too):
    * ``"any"`` — any reported variant in the gene;
    * ``"loss-of-function"`` — a reported variant in the gene whose
      interpretation is `pathogenic` or `likely-pathogenic` (the
      recorded schema carries no consequence field, so this is the
      documented proxy; sites needing precision publish rules naming
      the exact HGVS change);
    * any other string — exact match against the variant's reported
      hgvsC or hgvsP.
  :func:`evaluate_pgx` is the importable evaluation entry point
  (the coordinator wires it into the pharmacy verification flow —
  see the workstream integration note); `POST /genomics/pgx-check`
  exposes the same evaluation (capability `genomics.read`).

- **Gene cohort** (`GET /genomics/cohort?gene=`, capability
  `genomics.read`): patient references carrying ANY reported variant
  in the gene, plus the count — references and count ONLY, no variant
  detail, following the aggregate-safe posture of app.pophealth.
  (pophealth's own membership helper, `_members`, computes over the
  OMOP warehouse Person table from condition/lab definitions; it
  cannot see FHIR-store variant Observations, so membership here is
  computed from the FHIR store directly.)
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .configstore import published_config
from .models import ConfigStatus, FhirResource, StudioConfig, User
from .security import get_db, require_capability

router = APIRouter(prefix="/genomics", tags=["genomics"])

PGX_KEY = "pharmacogenomics"          # terminology/pharmacogenomics
PGX_SYSTEM = "urn:opencare:pharmacogenomics"

# FHIR genomics shape constants (see module docstring).
PANEL_SYSTEM = "http://loinc.org"
PANEL_CODE = "81247-9"                # genetic variant panel marker
CATEGORY_SYSTEM = ("http://terminology.hl7.org/CodeSystem/"
                   "observation-category")
GENE_COMPONENT_CODE = "48018-6"       # Gene studied [ID]
HGVSC_COMPONENT_CODE = "48004-6"      # DNA change (c.HGVS)
HGVSP_COMPONENT_CODE = "48005-3"      # Amino acid change (p.HGVS)
ZYGOSITY_COMPONENT_CODE = "53034-5"   # Allelic state
INTERPRETATION_COMPONENT_CODE = "variant-interpretation"

ZYGOSITIES = ("het", "hom", "hemi")
ZYGOSITY_CODINGS = {
    "het": ("LA6705-3", "Heterozygous"),
    "hom": ("LA6706-1", "Homozygous"),
    "hemi": ("LA6707-9", "Hemizygous"),
}
INTERPRETATIONS = ("pathogenic", "likely-pathogenic", "vus",
                   "likely-benign", "benign")
PGX_ACTIONS = ("avoid", "reduce", "flag")
# Interpretations treated as loss-of-function evidence for a rule
# whose `variant` is the "loss-of-function" token (see docstring).
_LOF_INTERPRETATIONS = ("pathogenic", "likely-pathogenic")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Published Studio configuration: terminology/pharmacogenomics
# ---------------------------------------------------------------------------

def default_pharmacogenomics_payload() -> dict:
    """EXAMPLE pharmacogenomic rules (synthetic-but-realistic).

    The `concepts` list exists so the payload satisfies the Studio
    terminology validator (code + display per rule drug, exactly the
    medication-monitoring precedent); the RULES are the `rules` list.
    Every rule's message is prefixed EXAMPLE and the description
    states plainly that sites replace this seed with their governed
    PGx source — this content is NOT a clinical knowledge base."""
    rules = [
        {"gene": "CYP2C19", "variant": "loss-of-function",
         "phenotype": "poor metabolizer",
         "drugCode": "309362", "drugDisplay": "Clopidogrel",
         "action": "flag",
         "message": ("EXAMPLE: CYP2C19 loss-of-function variant "
                     "reported — clopidogrel activation may be "
                     "reduced; flag for prescriber/pharmacist review "
                     "of antiplatelet therapy.")},
        {"gene": "DPYD", "variant": "loss-of-function",
         "phenotype": "dihydropyrimidine dehydrogenase deficiency",
         "drugCode": "4850", "drugDisplay": "Fluorouracil",
         "action": "avoid",
         "message": ("EXAMPLE: DPYD loss-of-function variant "
                     "reported — fluorouracil toxicity risk is "
                     "markedly increased; avoid fluorouracil pending "
                     "specialist review.")},
        {"gene": "CYP2D6", "variant": "loss-of-function",
         "phenotype": "poor metabolizer",
         "drugCode": "2670", "drugDisplay": "Codeine",
         "action": "flag",
         "message": ("EXAMPLE: CYP2D6 loss-of-function variant "
                     "reported — codeine may provide inadequate "
                     "analgesia (reduced conversion to morphine); "
                     "flag for prescriber review.")},
        {"gene": "SLCO1B1", "variant": "c.521T>C",
         "phenotype": "decreased transporter function",
         "drugCode": "36567", "drugDisplay": "Simvastatin",
         "action": "reduce",
         "message": ("EXAMPLE: SLCO1B1 c.521T>C reported — "
                     "simvastatin myopathy risk is increased; "
                     "reduce the simvastatin dose or choose an "
                     "alternative statin per the site guideline.")},
    ]
    return {
        "name": "Pharmacogenomic rules (EXAMPLE seed)",
        "system": PGX_SYSTEM,
        "description": (
            "EXAMPLE CONTENT — synthetic-but-realistic seed rules "
            "for development and evaluation ONLY. A deploying site "
            "MUST replace these with its own governed pharmacogenomic "
            "source (e.g. its CPIC-aligned rule set) through the "
            "Studio draft -> publish lifecycle before any clinical "
            "use. Rule shape: {gene, variant, phenotype, drugCode, "
            "action: avoid|reduce|flag, message}. `variant` matching: "
            "'any' = any reported variant in the gene; "
            "'loss-of-function' = a reported variant in the gene "
            "interpreted pathogenic / likely-pathogenic; any other "
            "value = exact HGVS (c. or p.) match."),
        "concepts": [
            {"code": r["drugCode"], "active": True,
             "display": f"{r['drugDisplay']} — {r['gene']} PGx rule "
                        f"(EXAMPLE)"}
            for r in rules
        ],
        "rules": rules,
    }


def _seed_published(session: Session, kind: str, key: str,
                    payload: dict) -> None:
    """Insert-if-absent published v1 seed (app.pharmacy pattern)."""
    existing = session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind,
                                   StudioConfig.key == key)).first()
    if existing is not None:
        return
    session.add(StudioConfig(kind=kind, key=key, version=1,
                             status=ConfigStatus.published,
                             payload=payload, created_by="system",
                             updated_by="system"))
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:{kind}/{key}@v1",
                     payload={"kind": kind, "key": key, "version": 1,
                              "seeded_default": True})


def ensure_genomics_defaults(session: Session) -> None:
    """Seed terminology/pharmacogenomics as published v1 (idempotent:
    an existing config of that kind/key is the truth and is never
    reset by a restart)."""
    _seed_published(session, "terminology", PGX_KEY,
                    default_pharmacogenomics_payload())
    session.commit()


def _pgx_rules(session: Session) -> list[dict]:
    """Published PGx rules; [] when none is published (the check is
    then inert — no rules, no findings, exactly the monitoring-hook
    posture in app.pharmacy)."""
    cfg = published_config(session, "terminology", PGX_KEY)
    if cfg is None:
        return []
    rules = (cfg.payload or {}).get("rules")
    if not isinstance(rules, list):
        return []
    return [r for r in rules
            if isinstance(r, dict) and r.get("gene")
            and r.get("drugCode")
            and r.get("action") in PGX_ACTIONS]


# ---------------------------------------------------------------------------
# Variant Observations: build + parse
# ---------------------------------------------------------------------------

def _component(code: str, display: str, value: dict,
               system: str = PANEL_SYSTEM) -> dict:
    return {"code": {"coding": [{"system": system, "code": code,
                                 "display": display}],
                     "text": display},
            **value}


def _variant_observation_body(patient_ref: str, gene: str, hgvs_c: str,
                              hgvs_p: str | None, zygosity: str,
                              interpretation: str,
                              source_lab: str | None) -> dict:
    la_code, la_display = ZYGOSITY_CODINGS[zygosity]
    components = [
        _component(GENE_COMPONENT_CODE, "Gene studied [ID]", {
            "valueCodeableConcept": {
                "coding": [{"system": "urn:opencare:genomics/gene",
                            "code": gene, "display": gene}],
                "text": gene}}),
        _component(HGVSC_COMPONENT_CODE, "DNA change (c.HGVS)",
                   {"valueString": hgvs_c}),
    ]
    if hgvs_p:
        components.append(
            _component(HGVSP_COMPONENT_CODE, "Amino acid change (p.HGVS)",
                       {"valueString": hgvs_p}))
    components.append(
        _component(ZYGOSITY_COMPONENT_CODE, "Allelic state", {
            "valueCodeableConcept": {
                "coding": [{"system": PANEL_SYSTEM, "code": la_code,
                            "display": la_display}],
                "text": zygosity}}))
    components.append(
        _component(INTERPRETATION_COMPONENT_CODE,
                   "Variant interpretation", {
                       "valueCodeableConcept": {
                           "coding": [{
                               "system": ("urn:opencare:genomics/"
                                          "interpretation"),
                               "code": interpretation,
                               "display": interpretation}],
                           "text": interpretation}},
                   system="urn:opencare:genomics"))
    body = {
        "resourceType": "Observation",
        "id": str(uuid.uuid4()),
        "status": "final",
        "category": [{"coding": [{"system": CATEGORY_SYSTEM,
                                  "code": "laboratory",
                                  "display": "Laboratory"}],
                      "text": "Laboratory"}],
        "code": {"coding": [{"system": PANEL_SYSTEM, "code": PANEL_CODE,
                             "display": "Genetic variant panel"}],
                 "text": f"{gene} reported variant"},
        "subject": {"reference": patient_ref},
        "effectiveDateTime": _now_iso(),
        "issued": _now_iso(),
        "component": components,
    }
    if source_lab:
        body["performer"] = [{"display": source_lab}]
    return body


def _component_value(body: dict, code: str) -> str | None:
    for comp in body.get("component") or []:
        if not isinstance(comp, dict):
            continue
        codings = (comp.get("code") or {}).get("coding") or []
        if not any(isinstance(c, dict) and c.get("code") == code
                   for c in codings):
            continue
        if isinstance(comp.get("valueString"), str):
            return comp["valueString"]
        concept = comp.get("valueCodeableConcept")
        if isinstance(concept, dict):
            if isinstance(concept.get("text"), str) and concept["text"]:
                return concept["text"]
            cc = concept.get("coding") or []
            if cc and isinstance(cc[0], dict) and cc[0].get("code"):
                return str(cc[0]["code"])
        return None
    return None


def _is_variant_observation(body: dict | None) -> bool:
    if not isinstance(body, dict):
        return False
    codings = (body.get("code") or {}).get("coding") or []
    return any(isinstance(c, dict) and c.get("code") == PANEL_CODE
               for c in codings)


def _variant_summary(row: FhirResource) -> dict:
    body = row.body or {}
    performer = body.get("performer") or []
    source_lab = None
    if performer and isinstance(performer[0], dict):
        source_lab = performer[0].get("display")
    return {
        "observation_id": row.resource_id,
        "patient_ref": row.patient_ref,
        "gene": _component_value(body, GENE_COMPONENT_CODE),
        "hgvs_c": _component_value(body, HGVSC_COMPONENT_CODE),
        "hgvs_p": _component_value(body, HGVSP_COMPONENT_CODE),
        "zygosity": _component_value(body, ZYGOSITY_COMPONENT_CODE),
        "interpretation": _component_value(
            body, INTERPRETATION_COMPONENT_CODE),
        "source_lab": source_lab,
        "status": body.get("status"),
    }


def _patient_variants(session: Session,
                      patient_ref: str) -> list[FhirResource]:
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Observation",
            FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.created_at.asc(),
                  FhirResource.id.asc())).all()
    return [r for r in rows if _is_variant_observation(r.body)]


def _patient_exists(session: Session, patient_ref: str) -> bool:
    patient_id = patient_ref.split("/", 1)[1] if "/" in patient_ref \
        else patient_ref
    return session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Patient",
            FhirResource.resource_id == patient_id)).first() is not None


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


def _require_str(payload: dict, *fields: str) -> str:
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise HTTPException(
        status_code=422,
        detail=f"Requires a non-empty '{fields[0]}'")


def _optional_str(payload: dict, field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be a string")
    return value.strip() or None


# ---------------------------------------------------------------------------
# PGx evaluation (importable — the pharmacy wiring calls this)
# ---------------------------------------------------------------------------

def _rule_matches(rule: dict, variant: dict) -> bool:
    if str(rule.get("gene")) != str(variant.get("gene")):
        return False
    token = rule.get("variant")
    if token is None or token == "any":
        return True
    if token == "loss-of-function":
        return variant.get("interpretation") in _LOF_INTERPRETATIONS
    return token in (variant.get("hgvs_c"), variant.get("hgvs_p"))


def evaluate_pgx(session: Session, patient_ref: str,
                 medication_codes) -> list[dict]:
    """Evaluate the published pharmacogenomics rules for one patient
    against a list of medication codes.

    Returns one finding per (variant, rule) match:
    ``{gene, variant, drug, action, message}`` where `variant` is the
    patient's reported HGVS c. change (falling back to the p. change),
    `drug` is the matched medication code, and `action` / `message`
    come from the governed rule. No published rules, no variants, or
    no matching drug -> []. Pure read: writes nothing."""
    codes = {str(c) for c in (medication_codes or []) if c is not None}
    if not codes:
        return []
    rules = [r for r in _pgx_rules(session)
             if str(r.get("drugCode")) in codes]
    if not rules:
        return []
    ref = patient_ref if "/" in str(patient_ref) \
        else f"Patient/{patient_ref}"
    findings: list[dict] = []
    for row in _patient_variants(session, ref):
        variant = _variant_summary(row)
        for rule in rules:
            if _rule_matches(rule, variant):
                findings.append({
                    "gene": variant.get("gene"),
                    "variant": variant.get("hgvs_c")
                    or variant.get("hgvs_p"),
                    "drug": str(rule.get("drugCode")),
                    "action": rule.get("action"),
                    "message": rule.get("message"),
                })
    return findings


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/variants", status_code=201)
async def record_variant(request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("genomics.work"))):
    """Record one REPORTED variant result (no variant calling — see
    the module docstring) as a FHIR Observation in the chart."""
    payload = await _json_body(request)
    patient_raw = _require_str(payload, "patientRef", "patient_ref",
                               "patient")
    patient_ref = patient_raw if "/" in patient_raw \
        else f"Patient/{patient_raw}"
    if not _patient_exists(session, patient_ref):
        raise HTTPException(status_code=404,
                            detail=f"{patient_ref} not found")
    gene = _require_str(payload, "gene")
    hgvs_c = _require_str(payload, "hgvsC", "hgvs_c")
    hgvs_p = _optional_str(payload, "hgvsP") or _optional_str(payload, "hgvs_p")
    zygosity_raw = _require_str(payload, "zygosity").lower()
    if zygosity_raw not in ZYGOSITIES:
        raise HTTPException(
            status_code=422,
            detail=f"'zygosity' must be one of {', '.join(ZYGOSITIES)}")
    interpretation_raw = _require_str(payload, "interpretation").lower()
    if interpretation_raw not in INTERPRETATIONS:
        raise HTTPException(
            status_code=422,
            detail=("'interpretation' must be one of "
                    f"{', '.join(INTERPRETATIONS)}"))
    source_lab = _optional_str(payload, "sourceLab") \
        or _optional_str(payload, "source_lab")

    body = _variant_observation_body(patient_ref, gene, hgvs_c, hgvs_p,
                                     zygosity_raw, interpretation_raw,
                                     source_lab)
    # Through the FHIR store: validated, projected, fhir.create-audited
    # and committed by create_resource itself.
    rendered = fhir.create_resource("Observation", body, session, user)
    append_audit(session, actor=user.username,
                 action="genomics.variant.record",
                 entity=f"Observation/{rendered['id']}",
                 payload={"patient_ref": patient_ref, "gene": gene,
                          "hgvs_c": hgvs_c, "hgvs_p": hgvs_p,
                          "zygosity": zygosity_raw,
                          "interpretation": interpretation_raw,
                          "source_lab": source_lab})
    session.commit()
    summary = {"observation_id": rendered["id"],
               "patient_ref": patient_ref, "gene": gene,
               "hgvs_c": hgvs_c, "hgvs_p": hgvs_p,
               "zygosity": zygosity_raw,
               "interpretation": interpretation_raw,
               "source_lab": source_lab, "status": "final"}
    return {"variant": summary, "observation": rendered}


@router.get("/patients/{patient_id}/variants")
def list_patient_variants(patient_id: str,
                          session: Session = Depends(get_db),
                          user: User = Depends(require_capability("genomics.read"))):
    """All reported variants recorded for one patient, oldest first."""
    patient_ref = f"Patient/{patient_id}"
    if not _patient_exists(session, patient_ref):
        raise HTTPException(status_code=404,
                            detail=f"{patient_ref} not found")
    variants = [_variant_summary(r)
                for r in _patient_variants(session, patient_ref)]
    return {"patient_ref": patient_ref, "variants": variants,
            "total": len(variants)}


@router.post("/pgx-check")
async def pgx_check(request: Request, session: Session = Depends(get_db),
                    user: User = Depends(require_capability("genomics.read"))):
    """Pharmacogenomic check: the patient's recorded variants x a
    medication code list, against the published PGx rules. Exposes
    :func:`evaluate_pgx` (the function the pharmacy verification flow
    calls directly)."""
    payload = await _json_body(request)
    patient_raw = _require_str(payload, "patientRef", "patient_ref",
                               "patient")
    patient_ref = patient_raw if "/" in patient_raw \
        else f"Patient/{patient_raw}"
    if not _patient_exists(session, patient_ref):
        raise HTTPException(status_code=404,
                            detail=f"{patient_ref} not found")
    raw_codes = payload.get("medicationCodes") \
        or payload.get("medication_codes") \
        or payload.get("medications") or []
    if not isinstance(raw_codes, list):
        raise HTTPException(
            status_code=422,
            detail="'medicationCodes' must be a list of medication codes")
    codes: list[str] = []
    for item in raw_codes:
        if isinstance(item, dict):
            code = item.get("code") or item.get("drugCode")
            if code is not None:
                codes.append(str(code))
        elif item is not None:
            codes.append(str(item))
    findings = evaluate_pgx(session, patient_ref, codes)
    append_audit(session, actor=user.username, action="genomics.pgx.check",
                 entity=patient_ref,
                 payload={"medication_codes": codes,
                          "finding_count": len(findings),
                          "actions": sorted({f["action"] for f in findings})})
    session.commit()
    return {"patient_ref": patient_ref, "medication_codes": codes,
            "findings": findings, "total": len(findings)}


@router.get("/cohort")
def gene_cohort(gene: str | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("genomics.read"))):
    """Research cohort by gene: patient refs carrying ANY reported
    variant in `gene`, plus the count. References + count ONLY — no
    variant detail leaves through this endpoint."""
    if not gene or not str(gene).strip():
        raise HTTPException(status_code=422,
                            detail="Cohort requires a 'gene' query parameter")
    gene = str(gene).strip()
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Observation")).all()
    refs = sorted({r.patient_ref for r in rows
                   if r.patient_ref and _is_variant_observation(r.body)
                   and _component_value(r.body or {},
                                        GENE_COMPONENT_CODE) == gene})
    append_audit(session, actor=user.username,
                 action="genomics.cohort.query", entity=f"gene:{gene}",
                 payload={"gene": gene, "count": len(refs)})
    session.commit()
    return {"gene": gene, "patient_refs": refs, "count": len(refs)}
