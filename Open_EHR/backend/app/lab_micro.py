"""LAB-D12 — Microbiology workup depth (Lab Depth pass, worker W6).

The depth layer over the Phase 6B staged culture machine
(app.lab_benches micro endpoints): the 6B machine carries the report
lifecycle (preliminary -> organism-identified -> susceptibility ->
final, visibility-tagged until finalize); THIS module carries the
workup CONTENT a real bench produces across that lifecycle:

- a structured Gram stain (governed morphology / quantity / WBC and
  epithelial adequacy) replacing the free-text preliminary on the
  depth surface — a POSITIVE blood-culture Gram stain opens a
  critical-call record that requires `notified_to` + `read_back`,
  the CAP mechanic app.lab already enforces for released critical
  reports (pending -> notified -> acknowledged);
- a real plating record (media, streak pattern, incubation
  conditions + start time) — the depth workup REFUSES to advance
  past plating without one (named 409, zero partial write);
- colony workup: quantitation (urine CFU/mL with computed bands)
  and, for blood cultures, bottle set + time-to-positivity;
- MULTIPLE isolates per culture, each with organism, identification
  METHOD (MALDI-TOF is a method VALUE — the instrument is an
  external leg and is never simulated here) and ID confidence where
  the governed method carries one;
- AST per isolate with MIC and/or zone-diameter values whose
  interpretation is COMPUTED from governed breakpoint tables — the
  breakpoint standard (CLSI / EUCAST) AND version are stamped on
  every AST entry; governed expert rules are evaluated over the
  isolate's AST and every firing carries a named rule trace that can
  force the final interpretation with the reason shown;
  selective / cascade reporting is governed: the released view shows
  the released set, the lab view shows the full set with the
  suppression reason on each suppressed entry;
- a cumulative antibiogram (CLSI M39 pattern): per organism /
  antibiotic %S over a governed period, first-isolate-per-patient
  deduplication, and a governed minimum isolate count below which
  %S is suppressed, never invented;
- the LAB-m3 MDRO alert: finalising a culture whose organism is
  flagged MDRO / reportable in governed config raises the alert
  into Infection Prevention through its existing intake (see
  `raise_mdrom_alerts` — the honest bound lives there), and the
  culture's workup record carries the alert reference. The hook is
  invoked from app.lab_benches.micro_finalize.

Configure, don't fork — every site-specific list is published Studio
config, read through app.configstore.published_config:
  terminology/micro-organisms      (app.lab; concepts may carry
                                    `mdro` / `reportable` flags —
                                    seeded all False: OFF)
  terminology/micro-antibiotics    (app.lab)
  terminology/micro-workup         (this module: vocabularies —
                                    Gram morphology/quantity, media,
                                    streak patterns, incubation
                                    atmospheres, ID methods, culture
                                    types, antibiogram minimum)
  terminology/micro-breakpoints    (this module: breakpoint tables
                                    by standard + version — seeded
                                    with NO tables: none published
                                    until the lab adopts one)
  terminology/micro-expert-rules   (this module: seeded empty)
  terminology/micro-selective-reporting (this module: seeded empty)
  terminology/micro-mdro-alerts    (this module: organism -> IP
                                    HAI-type mapping — seeded empty)

Storage: ONE new table (models.LabMicroWorkup) holding the workup
body as JSON per order — operational bench data, not a FHIR
resource, on the LabQcRun / LabCriticalNotification precedent. No
columns are added to any existing table, so no ensure hook is owed.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir, infection_prevention, lab
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .models import ConfigStatus, FhirResource, LabMicroWorkup, StudioConfig, User
from .security import get_db, require_capability

router = APIRouter(prefix="/lab/micro-depth", tags=["lab-micro-depth"])

BREAKPOINTS_KEY = "micro-breakpoints"
EXPERT_RULES_KEY = "micro-expert-rules"
SELECTIVE_KEY = "micro-selective-reporting"
MDRO_ALERTS_KEY = "micro-mdro-alerts"
WORKUP_VOCAB_KEY = "micro-workup"

ORGANISM_SYSTEM = "urn:opencare:micro-organism"


# ---------------------------------------------------------------------------
# Governed payload defaults (seeded published v1 by the coordinator's
# ensure wiring; see the INTEGRATION NOTE in the W6 report)
# ---------------------------------------------------------------------------

def default_workup_vocab_payload() -> dict:
    """Microbiology workup vocabularies — the governed enumerations
    every depth field validates against. ID methods carry
    `carriesConfidence` (MALDI-TOF identification reports a
    confidence score; a colony-morphology call does not) and, for
    MALDI-TOF, the governed `minConfidence` validity floor the
    research records (identifications below it are flagged
    below-threshold on the isolate, never silently upgraded)."""
    def opt(code, display, **extra):
        return {"code": code, "display": display, "active": True, **extra}
    return {
        "name": "OpenCare microbiology workup vocabularies",
        # `concepts` is non-empty so the payload also validates
        # through the shared Studio terminology validator when the
        # laboratory republishes it (that validator requires a
        # non-empty concepts list for every terminology key).
        "concepts": [{"code": "micro-workup",
                      "display": "Microbiology workup vocabularies",
                      "active": True}],
        "gramMorphologies": [
            opt("gram-positive-cocci-clusters", "Gram-positive cocci in clusters"),
            opt("gram-positive-cocci-chains", "Gram-positive cocci in chains/pairs"),
            opt("gram-negative-rods", "Gram-negative rods"),
            opt("gram-positive-rods", "Gram-positive rods"),
            opt("gram-negative-diplococci", "Gram-negative diplococci"),
            opt("yeast", "Yeast forms"),
            opt("mixed-morphologies", "Mixed morphologies"),
            opt("none-seen", "No organisms seen"),
        ],
        "quantities": [opt(c, d) for c, d in (
            ("rare", "Rare"), ("few", "Few"), ("moderate", "Moderate"),
            ("many", "Many"))],
        "cellBands": [opt(c, d) for c, d in (
            ("none", "None seen"), ("<10", "<10 per low-power field"),
            ("10-25", "10-25 per low-power field"),
            (">25", ">25 per low-power field"))],
        "adequacyValues": [opt(c, d) for c, d in (
            ("adequate", "Adequate"),
            ("contamination-likely", "Contamination likely (epithelial-predominant)"),
            ("inadequate", "Inadequate"))],
        "media": [opt(c, d) for c, d in (
            ("blood-agar", "Blood agar"), ("chocolate-agar", "Chocolate agar"),
            ("macconkey", "MacConkey agar"), ("cna", "Colistin-nalidixic acid agar"),
            ("chromogenic-uti", "Chromogenic UTI agar"),
            ("sabouraud", "Sabouraud agar"),
            ("anaerobic-blood-agar", "Anaerobic blood agar"))],
        "streakPatterns": [opt(c, d) for c, d in (
            ("four-quadrant", "Four-quadrant streak"),
            ("calibrated-loop", "Calibrated loop (quantitative)"),
            ("confluent", "Confluent / lawn"))],
        "atmospheres": [opt(c, d) for c, d in (
            ("ambient-air", "Ambient air"), ("co2", "5-10% CO2"),
            ("anaerobic", "Anaerobic"), ("microaerophilic", "Microaerophilic"))],
        "cultureTypes": [opt(c, d) for c, d in (
            ("blood", "Blood culture"), ("urine", "Urine culture"),
            ("respiratory", "Respiratory culture"), ("wound", "Wound culture"),
            ("stool", "Stool culture"), ("csf", "CSF culture"),
            ("other", "Other culture"))],
        "bottleTypes": [opt(c, d) for c, d in (
            ("aerobic", "Aerobic bottle"), ("anaerobic", "Anaerobic bottle"),
            ("paediatric", "Paediatric bottle"))],
        "idMethods": [
            opt("maldi-tof", "MALDI-TOF mass spectrometry",
                carriesConfidence=True, minConfidence=75.0),
            opt("biochemical-panel", "Biochemical panel",
                carriesConfidence=True),
            opt("manual-biochemical", "Manual biochemical tests",
                carriesConfidence=False),
            opt("colony-morphology", "Colony morphology / bench tests",
                carriesConfidence=False),
            opt("molecular-panel", "Syndromic molecular panel",
                carriesConfidence=False),
            opt("sequencing-16s", "16S rRNA sequencing",
                carriesConfidence=False),
        ],
        # CLSI M39 pattern: a %S figure is only published for a
        # species with at least this many first isolates in the
        # period. Governed here; the antibiogram endpoint accepts no
        # silent lowering below it (a caller-supplied value is used
        # only when it is >= this floor is NOT assumed — see the
        # endpoint: an explicit query value is the caller's declared
        # analysis choice and is echoed in the response).
        "antibiogramMinIsolates": 30,
        "antibiogramPeriodDays": 365,
    }


def default_breakpoints_payload() -> dict:
    """Breakpoint tables — seeded with NO tables on purpose: which
    standard (CLSI / EUCAST) and which version a laboratory reports
    against is that laboratory's governed go-live decision, so the
    seed publishes an empty table set and AST entries recorded
    before a table is published carry NO computed interpretation
    (the reason is shown, never a guessed S/I/R).

    Table shape (published by the lab through Studio):
      {"standard": "CLSI", "version": "M100-34",
       "entries": [{"organism": "<code>|*",
                    "antibiotic": "<code>",
                    "mic_s_max": 0.25, "mic_r_min": 2,
                    "zone_s_min": 21, "zone_r_max": 14}]}
    MIC <= mic_s_max -> S; MIC >= mic_r_min -> R; between -> I.
    Zone >= zone_s_min -> S; zone <= zone_r_max -> R; between -> I.
    """
    return {"name": "OpenCare microbiology breakpoint tables "
                    "(none adopted — publish a table to enable "
                    "computed AST interpretation)",
            "concepts": [
                {"code": "CLSI", "display": "CLSI M100", "active": True},
                {"code": "EUCAST",
                 "display": "EUCAST breakpoint tables", "active": True}],
            "tables": []}


def default_expert_rules_payload() -> dict:
    """Expert rules evaluated over organism + AST — seeded empty
    (the rule list is the laboratory's governed clinical content).

    Rule shape:
      {"code": "intrinsic-resistance-e-coli-ampicillin",
       "display": "Intrinsic resistance: ...",
       "organism": "<code>|*", "antibiotic": "<code>|*",
       "interpretation_in": ["S", "I"],      # trigger on computed
       "co_result": {"antibiotic": "<code>", # optional inducible
                     "interpretation_in": ["R"]},  # pattern leg
       "force_to": "R", "reason": "..."}
    A firing appends a named trace and forces the final
    interpretation to `force_to`."""
    return {"name": "OpenCare microbiology expert rules (none adopted)",
            "concepts": [
                {"code": "intrinsic-resistance",
                 "display": "Intrinsic resistance", "active": True},
                {"code": "inducible-resistance",
                 "display": "Inducible resistance", "active": True},
                {"code": "reporting-correction",
                 "display": "Reporting correction", "active": True}],
            "rules": []}


def default_selective_payload() -> dict:
    """Selective / cascade reporting suppression rules — seeded
    empty. Rule shape:
      {"organism": "<code>|*", "antibiotic": "<code>",
       "reason": "Cascade: reported only when the primary agent "
                 "is resistant"}"""
    return {"name": "OpenCare selective/cascade reporting rules "
                    "(none adopted)",
            "concepts": [
                {"code": "cascade", "display": "Cascade reporting",
                 "active": True},
                {"code": "selective", "display": "Selective reporting",
                 "active": True}],
            "rules": []}


def default_mdrom_alerts_payload() -> dict:
    """MDRO -> Infection Prevention alert mapping — seeded empty.
    The organism flag itself lives on the organism concept
    (`mdro` / `reportable` in terminology/micro-organisms, seeded
    all False). This mapping names, per flagged organism, the
    published HAI type the IP case intake files the alert under:
      {"alerts": [{"organism": "staphylococcus-aureus",
                   "hai_type": "mrsa"}]}
    IP cases carry NO organism field (that is SAFETY-M5's surface),
    so when no mapping names a valid HAI type the alert falls back
    to an isolation precaution with the organism named in its
    reason text — see raise_mdrom_alerts."""
    return {"name": "OpenCare MDRO alert mapping (none adopted)",
            "concepts": [
                {"code": "mdro", "display": "Multi-drug-resistant organism",
                 "active": True},
                {"code": "reportable", "display": "Reportable organism",
                 "active": True}],
            "alerts": []}


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


def ensure_lab_micro_defaults(session: Session) -> None:
    """Seed the LAB-D12 governed configs as published v1
    (idempotent). Coordinator wires this in app.main startup next to
    ensure_lab_defaults (see the W6 INTEGRATION NOTE)."""
    _seed_published(session, "terminology", WORKUP_VOCAB_KEY,
                    default_workup_vocab_payload())
    _seed_published(session, "terminology", BREAKPOINTS_KEY,
                    default_breakpoints_payload())
    _seed_published(session, "terminology", EXPERT_RULES_KEY,
                    default_expert_rules_payload())
    _seed_published(session, "terminology", SELECTIVE_KEY,
                    default_selective_payload())
    _seed_published(session, "terminology", MDRO_ALERTS_KEY,
                    default_mdrom_alerts_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors (published config, module defaults as fallback —
# the app.lab pattern: a disabled/never-seeded config degrades to the
# module default, which for breakpoints/rules is EMPTY, never a
# fabricated clinical table)
# ---------------------------------------------------------------------------

def _cfg(session: Session, key: str, default_fn) -> tuple[dict, int | None]:
    cfg = published_config(session, "terminology", key)
    if cfg is not None:
        return cfg.payload or {}, cfg.version
    return default_fn(), None


def _vocab(session: Session) -> dict:
    payload, _ = _cfg(session, WORKUP_VOCAB_KEY, default_workup_vocab_payload)
    return payload


def _codes(options) -> set[str]:
    return {o.get("code") for o in options or [] if isinstance(o, dict)}


def _organisms(session: Session) -> tuple[list[dict], int | None]:
    cfg = published_config(session, "terminology", lab.ORGANISMS_KEY)
    if cfg is not None:
        return (cfg.payload or {}).get("concepts") or [], cfg.version
    return default_organisms_fallback(), None


def default_organisms_fallback() -> list[dict]:
    return lab.default_organisms_payload().get("concepts") or []


def _organism(session: Session, code: str) -> dict | None:
    concepts, _ = _organisms(session)
    for concept in concepts:
        if isinstance(concept, dict) and concept.get("code") == code:
            return concept
    return None


def _antibiotics(session: Session) -> tuple[list[dict], int | None]:
    cfg = published_config(session, "terminology", lab.ANTIBIOTICS_KEY)
    if cfg is not None:
        return (cfg.payload or {}).get("concepts") or [], cfg.version
    return lab.default_antibiotics_payload().get("concepts") or [], None


def _antibiotic(session: Session, code: str) -> dict | None:
    concepts, _ = _antibiotics(session)
    for concept in concepts:
        if isinstance(concept, dict) and concept.get("code") == code:
            return concept
    return None


# ---------------------------------------------------------------------------
# Workup record access
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_body(order_id: str, patient_ref: str | None) -> dict:
    return {"order_id": order_id, "patient_ref": patient_ref,
            "culture_type": None, "plating": None, "gram_stain": None,
            "critical_call": None, "colony": None, "isolates": [],
            "mdro_alerts": [], "finalized_at": None}


def _workup_row(session: Session, order_id: str,
                create: bool = False,
                patient_ref: str | None = None) -> LabMicroWorkup | None:
    row = session.scalars(select(LabMicroWorkup).where(
        LabMicroWorkup.order_id == order_id)).first()
    if row is None and create:
        row = LabMicroWorkup(order_id=order_id,
                             patient_ref=patient_ref or "",
                             body=_empty_body(order_id, patient_ref))
        session.add(row)
        session.flush()
    return row


def _micro_order(session: Session, order_id: str) -> FhirResource:
    order = lab._order_row(session, order_id)
    concept = lab._order_code(session, order)
    if concept.get("bench") != "microbiology":
        raise HTTPException(
            status_code=422,
            detail=(f"Order {order_id} is on the "
                    f"'{concept.get('bench') or 'standard'}' bench, not "
                    f"'microbiology'"))
    return order


def _require_status(order: FhirResource, *statuses: str,
                    action: str) -> None:
    current = (order.body or {}).get("status")
    if current not in statuses:
        raise HTTPException(
            status_code=409,
            detail=(f"Cannot {action} order {order.resource_id} in status "
                    f"'{current}' (requires {' or '.join(statuses)})"))


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


def _require_enum(payload: dict, field: str, allowed: set[str],
                  label: str) -> str:
    value = payload.get(field)
    if value not in allowed:
        raise HTTPException(
            status_code=422,
            detail=(f"'{field}' must be one of the governed {label} "
                    f"values {sorted(allowed)} (got {value!r})"))
    return value


def _suppression_for(session: Session, organism_code: str,
                     antibiotic_code: str) -> dict | None:
    payload, _ = _cfg(session, SELECTIVE_KEY, default_selective_payload)
    for rule in payload.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("antibiotic") != antibiotic_code:
            continue
        if rule.get("organism") not in (organism_code, "*"):
            continue
        return {"suppressed": True,
                "reason": rule.get("reason") or
                "Suppressed by a governed selective-reporting rule"}
    return None


def _view(session: Session, body: dict, view: str) -> dict:
    """Render the workup body for the lab (full) or released
    (clinician-facing, selective reporting applied) view."""
    out = {k: body.get(k) for k in
           ("order_id", "patient_ref", "culture_type", "plating",
            "gram_stain", "critical_call", "colony", "mdro_alerts",
            "finalized_at")}
    # Notification internals stay staff-side: the released view never
    # carries who was phoned (the app.lab critical-reports precedent).
    if view == "released":
        out["critical_call"] = (
            {"state": body["critical_call"]["state"]}
            if body.get("critical_call") else None)
    isolates = []
    for isolate in body.get("isolates") or []:
        rendered = dict(isolate)
        ast = []
        for entry in isolate.get("ast") or []:
            suppression = _suppression_for(
                session, isolate["organism_code"], entry["antibiotic"])
            if view == "released" and suppression:
                ast.append({"antibiotic": entry["antibiotic"],
                            "suppressed": True,
                            "suppression_reason": suppression["reason"]})
            else:
                shown = dict(entry)
                shown["suppressed"] = bool(suppression)
                if suppression:
                    shown["suppression_reason"] = suppression["reason"]
                ast.append(shown)
        rendered["ast"] = ast
        isolates.append(rendered)
    out["isolates"] = isolates
    out["view"] = view
    return out


# ---------------------------------------------------------------------------
# AST computation: breakpoints + expert rules
# ---------------------------------------------------------------------------

def _breakpoint_entry(session: Session, standard: str, version: str,
                      organism_code: str,
                      antibiotic_code: str) -> dict | None:
    payload, _ = _cfg(session, BREAKPOINTS_KEY, default_breakpoints_payload)
    for table in payload.get("tables") or []:
        if not isinstance(table, dict):
            continue
        if table.get("standard") != standard or \
                str(table.get("version")) != str(version):
            continue
        for entry in table.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("antibiotic") != antibiotic_code:
                continue
            if entry.get("organism") not in (organism_code, "*"):
                continue
            return entry
    return None


def _compute_interpretation(entry_bp: dict, mic, zone) -> str | None:
    if mic is not None:
        if entry_bp.get("mic_s_max") is not None and \
                mic <= float(entry_bp["mic_s_max"]):
            return "S"
        if entry_bp.get("mic_r_min") is not None and \
                mic >= float(entry_bp["mic_r_min"]):
            return "R"
        if entry_bp.get("mic_s_max") is not None or \
                entry_bp.get("mic_r_min") is not None:
            return "I"
    if zone is not None:
        if entry_bp.get("zone_s_min") is not None and \
                zone >= float(entry_bp["zone_s_min"]):
            return "S"
        if entry_bp.get("zone_r_max") is not None and \
                zone <= float(entry_bp["zone_r_max"]):
            return "R"
        if entry_bp.get("zone_s_min") is not None or \
                entry_bp.get("zone_r_max") is not None:
            return "I"
    return None


def _evaluate_expert_rules(session: Session, organism_code: str,
                           ast: list[dict]) -> list[dict]:
    """Evaluate the governed expert rules over ONE isolate's AST.
    Mutates each entry's `expert_traces` / `final_interpretation`.
    A rule fires when the entry's computed interpretation is in the
    rule's trigger set (and, for inducible patterns, the named
    co-result on the SAME isolate is also in its trigger set)."""
    payload, _ = _cfg(session, EXPERT_RULES_KEY, default_expert_rules_payload)
    rules = [r for r in payload.get("rules") or [] if isinstance(r, dict)]
    by_antibiotic = {e["antibiotic"]: e for e in ast}
    for entry in ast:
        entry["expert_traces"] = []
        entry["final_interpretation"] = entry.get("computed_interpretation")
        computed = entry.get("computed_interpretation")
        if computed is None:
            continue
        for rule in rules:
            if rule.get("antibiotic") not in (entry["antibiotic"], "*"):
                continue
            if rule.get("organism") not in (organism_code, "*"):
                continue
            triggers = rule.get("interpretation_in") or []
            if triggers and computed not in triggers:
                continue
            co = rule.get("co_result") or {}
            if co:
                co_entry = by_antibiotic.get(co.get("antibiotic"))
                if co_entry is None:
                    continue
                co_triggers = co.get("interpretation_in") or []
                if co_triggers and \
                        co_entry.get("computed_interpretation") not in \
                        co_triggers:
                    continue
            force_to = rule.get("force_to")
            entry["expert_traces"].append({
                "rule_code": rule.get("code"),
                "rule_display": rule.get("display"),
                "reason": rule.get("reason"),
                "from_interpretation": computed,
                "to_interpretation": force_to})
            if force_to:
                entry["final_interpretation"] = force_to
    return ast


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------

@router.get("/config")
def micro_depth_config(session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.read"))):
    """Everything the workup surface renders from: vocabularies,
    organisms (with MDRO flags), antibiotics, and the adopted
    breakpoint tables / expert rules / selective rules — with the
    published version of each, so the UI can show exactly which
    governed content a computation used. Empty breakpoint/rule sets
    are returned as empty (none adopted), never synthesised."""
    vocab = _vocab(session)
    organisms, organisms_version = _organisms(session)
    antibiotics, antibiotics_version = _antibiotics(session)
    breakpoints, breakpoints_version = _cfg(
        session, BREAKPOINTS_KEY, default_breakpoints_payload)
    expert, expert_version = _cfg(
        session, EXPERT_RULES_KEY, default_expert_rules_payload)
    selective, selective_version = _cfg(
        session, SELECTIVE_KEY, default_selective_payload)
    return {
        "vocab": vocab,
        "organisms": organisms, "organisms_version": organisms_version,
        "antibiotics": antibiotics,
        "antibiotics_version": antibiotics_version,
        "breakpoint_tables": breakpoints.get("tables") or [],
        "breakpoints_version": breakpoints_version,
        "expert_rules": expert.get("rules") or [],
        "expert_rules_version": expert_version,
        "selective_rules": selective.get("rules") or [],
        "selective_rules_version": selective_version,
    }


# ---------------------------------------------------------------------------
# Plating record — the workup cannot advance past plating without it
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/plating", status_code=201)
async def record_plating(order_id: str, request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("lab.work"))):
    """Record the plating step for a collected culture: media,
    streak pattern, incubation conditions and the incubation start
    time. Recording plating on a COLLECTED order advances it to
    `plated` (order + specimen) — the status now means a recorded
    act, not a bare flip."""
    payload = await _json_body(request)
    order = _micro_order(session, order_id)
    _require_status(order, "collected", "plated",
                    action="record plating for")
    vocab = _vocab(session)
    media = payload.get("media")
    if not isinstance(media, list) or not media:
        raise HTTPException(status_code=422,
                            detail="'media' must be a non-empty list of "
                                   "governed media codes")
    bad = [m for m in media if m not in _codes(vocab.get("media"))]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=(f"Unknown media {bad}: not in the governed "
                    f"micro-workup vocabulary "
                    f"{sorted(_codes(vocab.get('media')))}"))
    streak = _require_enum(payload, "streak_pattern",
                           _codes(vocab.get("streakPatterns")),
                           "streak pattern")
    incubation = payload.get("incubation")
    if not isinstance(incubation, dict):
        raise HTTPException(status_code=422,
                            detail="'incubation' must be an object with "
                                   "temperature_c, atmosphere, "
                                   "duration_hours")
    atmosphere = incubation.get("atmosphere")
    if atmosphere not in _codes(vocab.get("atmospheres")):
        raise HTTPException(
            status_code=422,
            detail=(f"'incubation.atmosphere' must be one of "
                    f"{sorted(_codes(vocab.get('atmospheres')))}"))
    temperature = incubation.get("temperature_c")
    if not isinstance(temperature, (int, float)) or \
            not 15 <= float(temperature) <= 45:
        raise HTTPException(status_code=422,
                            detail="'incubation.temperature_c' must be a "
                                   "number between 15 and 45")
    duration = incubation.get("duration_hours")
    if not isinstance(duration, (int, float)) or float(duration) <= 0:
        raise HTTPException(status_code=422,
                            detail="'incubation.duration_hours' must be "
                                   "a positive number")
    started_at = payload.get("started_at") or _now_iso()
    try:
        datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422,
                            detail="'started_at' must be an ISO datetime")
    culture_type = payload.get("culture_type")
    if culture_type is not None and \
            culture_type not in _codes(vocab.get("cultureTypes")):
        raise HTTPException(
            status_code=422,
            detail=(f"'culture_type' must be one of "
                    f"{sorted(_codes(vocab.get('cultureTypes')))}"))
    patient_ref = (order.body or {}).get("subject", {}).get("reference")
    row = _workup_row(session, order_id, create=True,
                      patient_ref=patient_ref)
    body = dict(row.body or _empty_body(order_id, patient_ref))
    body["plating"] = {
        "media": list(media), "streak_pattern": streak,
        "incubation": {"temperature_c": float(temperature),
                       "atmosphere": atmosphere,
                       "duration_hours": float(duration)},
        "started_at": str(started_at),
        "recorded_by": user.username, "recorded_at": _now_iso()}
    if culture_type:
        body["culture_type"] = culture_type
    row.body = body
    if (order.body or {}).get("status") == "collected":
        lab._set_status(order, "plated")
        specimen = lab._specimen_row(session, order)
        if specimen is not None:
            lab._set_status(specimen, "plated")
    session.flush()
    append_audit(session, actor=user.username, action="lab.micro.plating",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"order_id": order_id, "media": list(media),
                          "streak_pattern": streak})
    session.commit()
    return {"workup": _view(session, body, "lab"),
            "order": fhir._render(order)}


def _require_plating(body: dict, action: str) -> None:
    if not body.get("plating"):
        raise HTTPException(
            status_code=409,
            detail=(f"Cannot {action}: no plating record exists for "
                    f"this culture. Record plating (media, streak "
                    f"pattern, incubation conditions and start time) "
                    f"before the workup advances past plating."))


# ---------------------------------------------------------------------------
# Structured Gram stain + the blood-culture critical call
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/gram-stain", status_code=201)
async def record_gram_stain(order_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("lab.work"))):
    """The structured Gram stain — the depth replacement for the
    free-text preliminary. Requires a plating record (409 without).
    Writes the staged preliminary Observation on the culture's
    DiagnosticReport through the bench machinery and advances a
    PLATED order to preliminary-reported. A positive Gram stain on
    a BLOOD culture opens the critical-call record (pending until
    notified with read-back)."""
    from . import lab_benches  # bench report machinery (no cycle:
    # lab_benches imports lab_micro only lazily, inside finalize)
    payload = await _json_body(request)
    order = _micro_order(session, order_id)
    _require_status(order, "plated", "preliminary-reported",
                    action="record a Gram stain for")
    patient_ref = (order.body or {}).get("subject", {}).get("reference")
    row = _workup_row(session, order_id, create=True,
                      patient_ref=patient_ref)
    body = dict(row.body or _empty_body(order_id, patient_ref))
    _require_plating(body, "record a Gram stain")
    vocab = _vocab(session)
    morphology = _require_enum(payload, "morphology",
                               _codes(vocab.get("gramMorphologies")),
                               "Gram-stain morphology")
    quantity = _require_enum(payload, "quantity",
                             _codes(vocab.get("quantities")), "quantity")
    wbc = _require_enum(payload, "wbc", _codes(vocab.get("cellBands")),
                        "WBC band")
    epithelial = _require_enum(payload, "epithelial_cells",
                               _codes(vocab.get("cellBands")),
                               "epithelial-cell band")
    adequacy = _require_enum(payload, "adequacy",
                             _codes(vocab.get("adequacyValues")),
                             "specimen adequacy")
    culture_type = payload.get("culture_type") or body.get("culture_type")
    if culture_type is not None and \
            culture_type not in _codes(vocab.get("cultureTypes")):
        raise HTTPException(
            status_code=422,
            detail=(f"'culture_type' must be one of "
                    f"{sorted(_codes(vocab.get('cultureTypes')))}"))
    if culture_type:
        body["culture_type"] = culture_type
    organisms_present = morphology != "none-seen"
    body["gram_stain"] = {
        "morphology": morphology, "quantity": quantity, "wbc": wbc,
        "epithelial_cells": epithelial, "adequacy": adequacy,
        "organisms_present": organisms_present,
        "comment": payload.get("comment"),
        "recorded_by": user.username, "recorded_at": _now_iso()}
    # The critical call: a positive blood-culture Gram stain is
    # phoned immediately (research §1.5) — the record starts pending
    # and only a notified_to + read_back notification closes it.
    critical_opened = False
    if body.get("culture_type") == "blood" and organisms_present and \
            not body.get("critical_call"):
        body["critical_call"] = {
            "state": "pending", "reason": "positive-blood-culture-gram-stain",
            "opened_at": _now_iso(), "notified_to": None,
            "read_back": None, "notified_by": None, "notified_at": None,
            "acknowledged_by": None, "acknowledged_at": None}
        critical_opened = True
    row.body = body
    # Mirror onto the staged culture report (creating it when this
    # is the first preliminary content) so the report the bench
    # finalises carries the Gram stain.
    report = lab_benches._bench_report(session, order, "MB")
    if report is None:
        concept = lab._order_code(session, order)
        report_body = {
            "resourceType": "DiagnosticReport", "id": str(uuid.uuid4()),
            "status": "preliminary",
            "category": [{"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                "code": "MB", "display": "Microbiology"}]}],
            "code": {"coding": [{"system": lab.LOINC,
                                 "code": concept["code"],
                                 "display": concept.get("display")}],
                     "text": concept.get("display")},
            "subject": {"reference": patient_ref},
            "basedOn": [{"reference": f"ServiceRequest/{order_id}"}],
            "effectiveDateTime": _now_iso(), "result": [],
            "meta": {"tag": [dict(fhir.LAB_UNRELEASED_TAG)]},
        }
        specimen = lab._specimen_row(session, order)
        if specimen is not None:
            report_body["specimen"] = [
                {"reference": f"Specimen/{specimen.resource_id}"}]
        created = fhir.create_resource("DiagnosticReport", report_body,
                                       session, user)
        report = fhir._get_row("DiagnosticReport", created["id"], session)
    summary = (f"Gram stain: {morphology}, {quantity}; WBC {wbc}; "
               f"epithelial {epithelial}; adequacy {adequacy}")
    obs = lab_benches._micro_observation(
        session, user, order, report,
        code_coding={"system": lab.LOINC, "code": "Gram stain",
                     "display": "Gram stain"},
        code_text="Gram stain (structured)",
        value_key="valueString", value=summary)
    lab_benches._append_to_report(session, report, obs["id"])
    if (order.body or {}).get("status") == "plated":
        lab._set_status(order, "preliminary-reported")
        specimen = lab._specimen_row(session, order)
        if specimen is not None:
            lab._set_status(specimen, "preliminary-reported")
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.micro.gram_stain",
                 entity=f"DiagnosticReport/{report.resource_id}",
                 payload={"order_id": order_id, "morphology": morphology,
                          "critical_call_opened": critical_opened})
    session.commit()
    return {"workup": _view(session, body, "lab"),
            "report_id": report.resource_id,
            "critical_call_opened": critical_opened}


@router.post("/orders/{order_id}/gram-stain/notify")
async def notify_gram_stain(order_id: str, request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("lab.verify"))):
    """Record the critical call for a positive blood-culture Gram
    stain — the app.lab critical-value pattern: `notified_to` is
    required and `read_back` must be true (422 otherwise, nothing
    recorded)."""
    payload = await _json_body(request)
    order = _micro_order(session, order_id)
    row = _workup_row(session, order_id)
    body = dict((row.body if row else None) or
                _empty_body(order_id, None))
    call = body.get("critical_call")
    if not call:
        raise HTTPException(
            status_code=400,
            detail=(f"Order {order_id} has no open Gram-stain critical "
                    f"call (a critical call opens only for a positive "
                    f"blood-culture Gram stain)"))
    if call.get("state") in ("notified", "acknowledged"):
        raise HTTPException(
            status_code=409,
            detail=f"Gram-stain critical call already notified "
                   f"(state '{call.get('state')}')")
    notified_to = payload.get("notified_to")
    if not isinstance(notified_to, str) or not notified_to.strip():
        raise HTTPException(status_code=422,
                            detail="Notification requires 'notified_to' "
                                   "(who was notified)")
    if payload.get("read_back") is not True:
        raise HTTPException(
            status_code=422,
            detail=("Critical-value notification requires read-back "
                    "confirmation: 'read_back' must be true (the "
                    "receiver read the result back)"))
    call = dict(call)
    call.update({"state": "notified", "notified_to": notified_to.strip(),
                 "read_back": True, "notified_by": user.username,
                 "notified_at": _now_iso()})
    body["critical_call"] = call
    row.body = body
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.micro.gram_stain.notify",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"order_id": order_id,
                          "notified_to": call["notified_to"],
                          "read_back": True})
    session.commit()
    return {"workup": _view(session, body, "lab")}


@router.post("/orders/{order_id}/gram-stain/acknowledge")
def acknowledge_gram_stain(order_id: str,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.verify"))):
    """Acknowledge a notified Gram-stain critical call (pending ->
    notified -> acknowledged, in order; 409 out of order)."""
    order = _micro_order(session, order_id)
    row = _workup_row(session, order_id)
    body = dict((row.body if row else None) or
                _empty_body(order_id, None))
    call = body.get("critical_call")
    if not call or call.get("state") == "pending":
        raise HTTPException(
            status_code=409,
            detail="Gram-stain critical call has not been notified "
                   "yet; acknowledgement follows the notification call")
    if call.get("state") == "acknowledged":
        raise HTTPException(status_code=409,
                            detail="Gram-stain critical call already "
                                   "acknowledged")
    call = dict(call)
    call.update({"state": "acknowledged",
                 "acknowledged_by": user.username,
                 "acknowledged_at": _now_iso()})
    body["critical_call"] = call
    row.body = body
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.micro.gram_stain.acknowledge",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"order_id": order_id})
    session.commit()
    return {"workup": _view(session, body, "lab")}


# ---------------------------------------------------------------------------
# Colony workup
# ---------------------------------------------------------------------------

def _urine_band(cfu_ml: float) -> str:
    if cfu_ml < 10_000:
        return "<10,000 CFU/mL (no significant growth band)"
    if cfu_ml < 100_000:
        return "10,000-99,999 CFU/mL"
    return ">=100,000 CFU/mL"


@router.post("/orders/{order_id}/colony", status_code=201)
async def record_colony(order_id: str, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    """Colony workup content: quantitation (urine CFU/mL with the
    band computed server-side; other sources a governed growth
    amount) and, for blood cultures, the bottle set and
    time-to-positivity."""
    payload = await _json_body(request)
    order = _micro_order(session, order_id)
    _require_status(order, "plated", "preliminary-reported",
                    "organism-identified",
                    action="record colony workup for")
    patient_ref = (order.body or {}).get("subject", {}).get("reference")
    row = _workup_row(session, order_id, create=True,
                      patient_ref=patient_ref)
    body = dict(row.body or _empty_body(order_id, patient_ref))
    _require_plating(body, "record colony workup")
    culture_type = body.get("culture_type") or payload.get("culture_type")
    colony: dict = {"recorded_by": user.username,
                    "recorded_at": _now_iso()}
    cfu = payload.get("cfu_ml")
    if cfu is not None:
        if not isinstance(cfu, (int, float)) or float(cfu) < 0:
            raise HTTPException(status_code=422,
                                detail="'cfu_ml' must be a non-negative "
                                       "number")
        colony["cfu_ml"] = float(cfu)
        colony["cfu_band"] = _urine_band(float(cfu))
    growth = payload.get("growth_amount")
    if growth is not None:
        vocab = _vocab(session)
        if growth not in _codes(vocab.get("quantities")):
            raise HTTPException(
                status_code=422,
                detail=(f"'growth_amount' must be one of "
                        f"{sorted(_codes(vocab.get('quantities')))}"))
        colony["growth_amount"] = growth
    if cfu is None and growth is None:
        raise HTTPException(status_code=422,
                            detail="Colony workup requires quantitation: "
                                   "'cfu_ml' (urine) or 'growth_amount'")
    bottle = payload.get("bottle")
    if culture_type == "blood" or bottle is not None:
        if not isinstance(bottle, dict):
            raise HTTPException(
                status_code=422,
                detail="Blood-culture colony workup requires 'bottle' "
                       "{set_id, bottle_type, "
                       "time_to_positivity_hours}")
        vocab = _vocab(session)
        if bottle.get("bottle_type") not in _codes(vocab.get("bottleTypes")):
            raise HTTPException(
                status_code=422,
                detail=(f"'bottle.bottle_type' must be one of "
                        f"{sorted(_codes(vocab.get('bottleTypes')))}"))
        ttp = bottle.get("time_to_positivity_hours")
        if not isinstance(ttp, (int, float)) or float(ttp) <= 0:
            raise HTTPException(
                status_code=422,
                detail="'bottle.time_to_positivity_hours' must be a "
                       "positive number")
        if not isinstance(bottle.get("set_id"), str) or \
                not bottle["set_id"].strip():
            raise HTTPException(status_code=422,
                                detail="'bottle.set_id' is required")
        colony["bottle"] = {"set_id": bottle["set_id"].strip(),
                            "bottle_type": bottle["bottle_type"],
                            "time_to_positivity_hours": float(ttp)}
    body["colony"] = colony
    row.body = body
    session.flush()
    append_audit(session, actor=user.username, action="lab.micro.colony",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"order_id": order_id,
                          "cfu_ml": colony.get("cfu_ml")})
    session.commit()
    return {"workup": _view(session, body, "lab")}


# ---------------------------------------------------------------------------
# Isolates + AST
# ---------------------------------------------------------------------------

@router.post("/orders/{order_id}/isolates", status_code=201)
async def add_isolate(order_id: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.work"))):
    """Add ONE isolate to the culture (a culture carries as many as
    the bench recovers). The organism must be in the published
    terminology; the ID method is a governed value — MALDI-TOF is
    recorded as a method, the instrument itself is an external leg
    and is never simulated. Methods that carry a confidence require
    one (422 without); methods that do not carry one refuse one."""
    payload = await _json_body(request)
    order = _micro_order(session, order_id)
    _require_status(order, "plated", "preliminary-reported",
                    "organism-identified", "susceptibility-reported",
                    action="add an isolate to")
    patient_ref = (order.body or {}).get("subject", {}).get("reference")
    row = _workup_row(session, order_id, create=True,
                      patient_ref=patient_ref)
    body = dict(row.body or _empty_body(order_id, patient_ref))
    _require_plating(body, "add an isolate")
    code = payload.get("organism_code") or payload.get("organism")
    concept = _organism(session, str(code)) if code else None
    if concept is None:
        raise HTTPException(
            status_code=422,
            detail=(f"Organism '{code}' is not in the published "
                    f"micro-organisms terminology"))
    if concept.get("active") is False:
        raise HTTPException(status_code=422,
                            detail=f"Organism '{code}' is inactive")
    vocab = _vocab(session)
    methods = {m.get("code"): m for m in vocab.get("idMethods") or []
               if isinstance(m, dict)}
    method_code = payload.get("id_method")
    method = methods.get(method_code)
    if method is None:
        raise HTTPException(
            status_code=422,
            detail=(f"'id_method' must be one of the governed ID "
                    f"methods {sorted(methods)} (got {method_code!r})"))
    confidence = payload.get("id_confidence")
    if method.get("carriesConfidence"):
        if not isinstance(confidence, (int, float)) or \
                not 0 <= float(confidence) <= 100:
            raise HTTPException(
                status_code=422,
                detail=(f"ID method '{method_code}' carries a "
                        f"confidence score: 'id_confidence' (0-100) "
                        f"is required"))
        confidence = float(confidence)
    elif confidence is not None:
        raise HTTPException(
            status_code=422,
            detail=(f"ID method '{method_code}' does not carry a "
                    f"confidence score; 'id_confidence' must not be "
                    f"supplied"))
    isolate = {
        "isolate_id": str(uuid.uuid4()),
        "organism_code": concept["code"],
        "organism_display": concept.get("display"),
        "id_method": method_code,
        "id_confidence": confidence,
        "id_below_threshold": bool(
            method.get("minConfidence") is not None and
            confidence is not None and
            confidence < float(method["minConfidence"])),
        "ast": [],
        "recorded_by": user.username, "recorded_at": _now_iso()}
    # NB: assign a NEW list — mutating the nested list of a shallow
    # dict copy in place is invisible to the JSON column's dirty
    # tracking and the isolate would silently not persist.
    body["isolates"] = list(body.get("isolates") or []) + [isolate]
    row.body = body
    if (order.body or {}).get("status") == "preliminary-reported":
        lab._set_status(order, "organism-identified")
        specimen = lab._specimen_row(session, order)
        if specimen is not None:
            lab._set_status(specimen, "organism-identified")
    session.flush()
    append_audit(session, actor=user.username, action="lab.micro.isolate",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"order_id": order_id,
                          "organism": concept["code"],
                          "id_method": method_code,
                          "isolate_id": isolate["isolate_id"]})
    session.commit()
    return {"workup": _view(session, body, "lab"),
            "isolate_id": isolate["isolate_id"]}


@router.delete("/orders/{order_id}/isolates/{isolate_id}")
def remove_isolate(order_id: str, isolate_id: str,
                   session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.work"))):
    """Remove an isolate recorded in error — refused once the
    culture is final (a final report is corrected by amendment,
    never by silent deletion)."""
    order = _micro_order(session, order_id)
    if (order.body or {}).get("status") == "final":
        raise HTTPException(status_code=409,
                            detail="Culture is final; isolates cannot "
                                   "be removed (use the amend path)")
    row = _workup_row(session, order_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"No micro workup for order {order_id}")
    body = dict(row.body or {})
    isolates = body.get("isolates") or []
    kept = [i for i in isolates if i.get("isolate_id") != isolate_id]
    if len(kept) == len(isolates):
        raise HTTPException(status_code=404,
                            detail=f"Isolate {isolate_id} not found")
    body["isolates"] = kept
    row.body = body
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.micro.isolate.remove",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"order_id": order_id,
                          "isolate_id": isolate_id})
    session.commit()
    return {"workup": _view(session, body, "lab")}


@router.post("/orders/{order_id}/isolates/{isolate_id}/ast",
             status_code=201)
async def record_ast(order_id: str, isolate_id: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.work"))):
    """Record AST entries for ONE isolate. Each entry names its
    breakpoint standard + version; the interpretation is COMPUTED
    from the published table for that standard/version (the same
    MIC can legitimately read differently under two versions —
    that is the point of stamping both). An entry with no matching
    published breakpoint is stored with a null interpretation and
    the reason shown — never a guessed S/I/R. Expert rules are then
    evaluated over the isolate's whole AST."""
    payload = await _json_body(request)
    entries = payload.get("results")
    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=422,
                            detail="'results' must be a non-empty list "
                                   "of {antibiotic, mic?, zone_mm?, "
                                   "standard, version}")
    order = _micro_order(session, order_id)
    _require_status(order, "organism-identified",
                    "susceptibility-reported",
                    action="record AST for")
    row = _workup_row(session, order_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"No micro workup for order {order_id}")
    body = dict(row.body or {})
    isolate = next((i for i in body.get("isolates") or []
                    if i.get("isolate_id") == isolate_id), None)
    if isolate is None:
        raise HTTPException(status_code=404,
                            detail=f"Isolate {isolate_id} not found")
    new_entries = []
    for idx, item in enumerate(entries):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422,
                                detail=f"results[{idx}] must be an object")
        abx_code = item.get("antibiotic") or item.get("code")
        abx = _antibiotic(session, str(abx_code)) if abx_code else None
        if abx is None:
            raise HTTPException(
                status_code=422,
                detail=(f"results[{idx}] antibiotic '{abx_code}' is not "
                        f"in the published micro-antibiotics "
                        f"terminology"))
        standard = item.get("standard")
        version = item.get("version")
        if standard not in ("CLSI", "EUCAST") or not version:
            raise HTTPException(
                status_code=422,
                detail=(f"results[{idx}] requires 'standard' (CLSI or "
                        f"EUCAST) and 'version' — every AST entry is "
                        f"stamped with the breakpoint standard and "
                        f"version it was interpreted against"))
        mic = item.get("mic")
        zone = item.get("zone_mm")
        if mic is None and zone is None:
            raise HTTPException(
                status_code=422,
                detail=(f"results[{idx}] requires at least one of "
                        f"'mic' (mg/L) or 'zone_mm' (mm)"))
        if mic is not None and (not isinstance(mic, (int, float))
                                or float(mic) <= 0):
            raise HTTPException(status_code=422,
                                detail=f"results[{idx}].mic must be a "
                                       f"positive number")
        if zone is not None and (not isinstance(zone, (int, float))
                                 or float(zone) < 0):
            raise HTTPException(status_code=422,
                                detail=f"results[{idx}].zone_mm must be "
                                       f"a non-negative number")
        bp = _breakpoint_entry(session, standard, str(version),
                               isolate["organism_code"], abx["code"])
        computed = _compute_interpretation(bp, mic, zone) if bp else None
        new_entries.append({
            "antibiotic": abx["code"],
            "antibiotic_display": abx.get("display"),
            "mic": float(mic) if mic is not None else None,
            "zone_mm": float(zone) if zone is not None else None,
            "standard": standard, "version": str(version),
            "computed_interpretation": computed,
            "interpretation_note": (
                None if computed is not None else
                f"No published {standard} {version} breakpoint entry "
                f"for {isolate['organism_code']} / {abx['code']}; "
                f"interpretation not computed"),
            "expert_traces": [], "final_interpretation": computed,
            "recorded_by": user.username, "recorded_at": _now_iso()})
    isolate = dict(isolate)
    isolate["ast"] = _evaluate_expert_rules(
        session, isolate["organism_code"],
        list(isolate.get("ast") or []) + new_entries)
    body["isolates"] = [isolate if i.get("isolate_id") == isolate_id
                        else i for i in body.get("isolates") or []]
    row.body = body
    if (order.body or {}).get("status") == "organism-identified":
        lab._set_status(order, "susceptibility-reported")
        specimen = lab._specimen_row(session, order)
        if specimen is not None:
            lab._set_status(specimen, "susceptibility-reported")
    session.flush()
    append_audit(session, actor=user.username, action="lab.micro.ast",
                 entity=f"ServiceRequest/{order_id}",
                 payload={"order_id": order_id,
                          "isolate_id": isolate_id,
                          "result_count": len(new_entries)})
    session.commit()
    return {"workup": _view(session, body, "lab")}


# ---------------------------------------------------------------------------
# Workup read + antibiogram
# ---------------------------------------------------------------------------

@router.get("/orders/{order_id}/workup")
def get_workup(order_id: str,
               view: str = Query(default="lab"),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability("lab.read"))):
    if view not in ("lab", "released"):
        raise HTTPException(status_code=422,
                            detail="'view' must be 'lab' or 'released'")
    order = _micro_order(session, order_id)
    row = _workup_row(session, order_id)
    body = (row.body if row else None) or _empty_body(
        order_id, (order.body or {}).get("subject", {}).get("reference"))
    return {"workup": _view(session, body, view),
            "order_status": (order.body or {}).get("status")}


@router.get("/antibiogram")
def antibiogram(from_: str | None = Query(default=None, alias="from"),
                to: str | None = Query(default=None),
                min_isolates: int | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("lab.read"))):
    """Cumulative antibiogram (CLSI M39 pattern) over FINALISED
    culture workups in the period: first isolate per patient per
    organism only (deduplication), per organism/antibiotic %S from
    final interpretations, and groups under the governed minimum
    isolate count are returned with pct_s suppressed — a small-n
    percentage is never published as if it were a rate."""
    vocab = _vocab(session)
    governed_min = int(vocab.get("antibiogramMinIsolates") or 30)
    period_days = int(vocab.get("antibiogramPeriodDays") or 365)
    today = datetime.now(timezone.utc).date()
    end = today
    start = today - timedelta(days=period_days)
    if from_:
        try:
            start = date.fromisoformat(from_)
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="'from' must be an ISO date")
    if to:
        try:
            end = date.fromisoformat(to)
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="'to' must be an ISO date")
    effective_min = governed_min if min_isolates is None \
        else int(min_isolates)
    rows = session.scalars(select(LabMicroWorkup).order_by(
        LabMicroWorkup.created_at.asc(), LabMicroWorkup.id.asc())).all()
    # First isolate per (patient, organism): earliest finalised
    # workup in the period wins; later isolates of the same organism
    # for the same patient are duplicates for antibiogram purposes.
    seen: set[tuple[str, str]] = set()
    counts: dict[tuple[str, str], dict] = {}
    for row in rows:
        body = row.body or {}
        finalized_at = body.get("finalized_at")
        if not finalized_at:
            continue
        try:
            finalized_day = datetime.fromisoformat(
                str(finalized_at).replace("Z", "+00:00")).date()
        except ValueError:
            continue
        if not (start <= finalized_day <= end):
            continue
        patient = body.get("patient_ref") or row.patient_ref
        for isolate in body.get("isolates") or []:
            key = (patient, isolate.get("organism_code"))
            if key in seen:
                continue
            seen.add(key)
            for entry in isolate.get("ast") or []:
                final = entry.get("final_interpretation")
                if final not in ("S", "I", "R"):
                    continue
                group = counts.setdefault(
                    (isolate["organism_code"], entry["antibiotic"]),
                    {"organism": isolate["organism_code"],
                     "organism_display": isolate.get("organism_display"),
                     "antibiotic": entry["antibiotic"],
                     "antibiotic_display": entry.get("antibiotic_display"),
                     "isolates": 0, "susceptible": 0})
                group["isolates"] += 1
                if final == "S":
                    group["susceptible"] += 1
    items = []
    for group in counts.values():
        item = dict(group)
        if group["isolates"] < effective_min:
            item["pct_s"] = None
            item["suppressed"] = True
            item["suppression_reason"] = (
                f"Below the minimum isolate count ({effective_min}) "
                f"for a published %S (CLSI M39 pattern)")
        else:
            item["pct_s"] = round(
                100.0 * group["susceptible"] / group["isolates"], 1)
            item["suppressed"] = False
            item["suppression_reason"] = None
        items.append(item)
    items.sort(key=lambda i: (i["organism"], i["antibiotic"]))
    return {"items": items, "total": len(items),
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            "min_isolates": effective_min,
            "governed_min_isolates": governed_min,
            "deduplication": "first-isolate-per-patient-per-organism"}


# ---------------------------------------------------------------------------
# LAB-m3 — MDRO / reportable-organism alert into Infection Prevention
# ---------------------------------------------------------------------------

def raise_mdrom_alerts(session: Session, user: User,
                       order: FhirResource, report: FhirResource,
                       organism_codes: list[str]) -> list[dict]:
    """File the MDRO alert for a finalising culture. Called by
    app.lab_benches.micro_finalize in the finalize transaction.

    Mechanism + HONEST BOUND: Infection Prevention's case intake
    (POST /infection/cases) validates the type against the
    published HAI definitions and the InfectionCase row carries NO
    organism field — adding one is SAFETY-M5's surface, not D12's.
    So: for a flagged organism with a governed mapping
    (terminology/micro-mdro-alerts) naming a published HAI type,
    the alert is filed as an InfectionCase through that intake's
    own validation, with the organism NAMED in the case `note` and
    `source_ref` pointing at the culture report. For a flagged
    organism with no such mapping, the alert falls back to IP's
    other intake — an isolation precaution (contact) whose `reason`
    names the organism. Either way the alert reference is recorded
    on the culture's workup record and returned to the caller.
    Non-flagged organisms raise nothing."""
    flagged = []
    for code in dict.fromkeys(organism_codes):
        concept = _organism(session, code)
        if concept and (concept.get("mdro") or concept.get("reportable")):
            flagged.append(concept)
    if not flagged:
        return []
    mapping_payload, _ = _cfg(session, MDRO_ALERTS_KEY,
                              default_mdrom_alerts_payload)
    mappings = {a.get("organism"): a.get("hai_type")
                for a in mapping_payload.get("alerts") or []
                if isinstance(a, dict)}
    hai_types = infection_prevention._hai_types(session)
    patient_ref = (order.body or {}).get("subject", {}).get("reference")
    today = datetime.now(timezone.utc).date().isoformat()
    alerts = []
    for concept in flagged:
        code = concept["code"]
        display = concept.get("display") or code
        hai_type = mappings.get(code)
        if hai_type and hai_type in hai_types:
            case = infection_prevention.InfectionCase(
                case_id=str(uuid.uuid4()), hai_type=hai_type,
                patient_ref=patient_ref, onset_date=today,
                source_ref=f"DiagnosticReport/{report.resource_id}",
                unit=None, status="reported",
                note=(f"MDRO alert (LAB-m3): {display} ({code}) "
                      f"finalised on microbiology culture order "
                      f"{order.resource_id}. Organism named here "
                      f"because the IP case carries no organism "
                      f"field (SAFETY-M5)."),
                created_by=user.username)
            session.add(case)
            session.flush()
            append_audit(
                session, actor=user.username,
                action="infection.case.create",
                entity=f"InfectionCase/{case.case_id}",
                payload={"type": hai_type, "patient": patient_ref,
                         "onset_date": today,
                         "source_ref": case.source_ref,
                         "mdrom_alert": True, "organism": code})
            alert = {"kind": "infection-case", "ref": case.case_id,
                     "hai_type": hai_type, "organism": code,
                     "mechanism": "hai-case-intake"}
        else:
            precaution = infection_prevention.IsolationPrecaution(
                precaution_id=str(uuid.uuid4()),
                patient_ref=patient_ref, precaution_type="contact",
                reason=(f"MDRO alert (LAB-m3): {display} ({code}) "
                        f"finalised on microbiology culture order "
                        f"{order.resource_id}; no governed HAI-type "
                        f"mapping published for this organism, so "
                        f"the alert is filed as a precaution with "
                        f"the organism named in this reason."),
                unit=None, status="active", started_on=today,
                created_by=user.username)
            session.add(precaution)
            session.flush()
            append_audit(
                session, actor=user.username,
                action="infection.precaution.start",
                entity=f"IsolationPrecaution/{precaution.precaution_id}",
                payload={"type": "contact", "patient": patient_ref,
                         "mdrom_alert": True, "organism": code})
            alert = {"kind": "isolation-precaution",
                     "ref": precaution.precaution_id,
                     "hai_type": None, "organism": code,
                     "mechanism": "precaution-intake-fallback"}
        alerts.append(alert)
    # Record the alert reference(s) on the culture record itself.
    row = _workup_row(session, order.resource_id, create=True,
                      patient_ref=patient_ref)
    body = dict(row.body or _empty_body(order.resource_id, patient_ref))
    body["mdro_alerts"] = list(body.get("mdro_alerts") or []) + alerts
    body["finalized_at"] = _now_iso()
    row.body = body
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.micro.mdrom_alert",
                 entity=f"DiagnosticReport/{report.resource_id}",
                 payload={"order_id": order.resource_id,
                          "alerts": alerts})
    return alerts


def mark_finalized(session: Session, order: FhirResource) -> None:
    """Stamp finalized_at on the workup record at finalisation
    (the antibiogram's period key) even when no MDRO alert fires."""
    patient_ref = (order.body or {}).get("subject", {}).get("reference")
    row = _workup_row(session, order.resource_id, create=True,
                      patient_ref=patient_ref)
    body = dict(row.body or _empty_body(order.resource_id, patient_ref))
    body["finalized_at"] = _now_iso()
    row.body = body
    session.flush()
