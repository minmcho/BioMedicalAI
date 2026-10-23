"""Lab Depth W3 (LAB-D7) — autoverification maturity.

Builds ON the Phase 6B auto-verification in app.lab without weakening
or replacing it: the catalog `autoVerify` path still only VERIFIES
(never releases) exactly as before, and runs whenever no bench rule
set governs. This module adds the mature-lab layer the reality
check found missing (report §1.3 / §3.3 LAB-D7):

- **Governed rule sets per analyte/bench.** A rule set is published
  Studio configuration — terminology/lab-autoverify, one concept per
  bench (code = the bench code from terminology/lab-benches), the
  rule list riding as concept data. Seeded as a DRAFT template only:
  NO bench auto-releases until ITS rule set is published with
  `enabled: true` through the Studio draft -> publish flow — the
  Wave A / C-AI governance pattern, not a global switch.
- **Rule taxonomy** (the published Krasowski-style set): AMR bounds,
  interference (HIL-style) flags from the analyser, CONDITIONAL delta
  logic (the delta rule only bites when its stated condition holds),
  cross-analyte consistency traps, and absurd-value traps — plus the
  standing gates every evaluation always runs and traces: the
  report-level QC gate (app.lab.qc_blockers, unchanged), the W4
  instrument-path lock (app.lab_qc), and the bench's critical-value
  posture (`holdOnCritical`, governed per bench; the seeded template
  holds criticals — a bench that wants criticals to autoverify when
  every other rule passes, the mature-lab pattern, publishes so).
- **Auto-RELEASE under rules.** A result set that passes every rule
  in its bench's published set is verified AND released by
  "system:auto-verify" (the Phase 6B audit actor), through the same
  _release_now mutation the human release endpoint uses. A result
  set that fails any rule is HELD: nothing is verified or released,
  the draft stays in the verification queue for the unchanged manual
  verify/release path, and the hold is recorded here with its full
  rule trace.
- **Rule traces everywhere.** Every evaluation under a published set
  — release or hold — is recorded in lab_autoverify_events with the
  per-rule trace (which rules evaluated, which fired, with values).
- **CAP GEN.43890 parity.** The delta rule evaluates through
  app.lab._delta_flag — the identical function the manual
  verification queue uses — and :func:`evaluate_autoverification`
  is the ONE evaluation function: the auto path in
  app.lab._create_results and the dry-run endpoint below both call
  it, so an auto-released and a manually-reviewed result are judged
  by the same code.
- **Rate monitoring.** Autoverification rate per bench (released /
  held / total over a window) is computed from the recorded events —
  the metric mature laboratories manage by.

This is deterministic rule evaluation over governed configuration.
It is NOT an AI agent: no model is consulted, no C-AI surface is
touched, and nothing here is runnable as an agent.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (Boolean, DateTime, Integer, JSON, String, Text,
                        select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import ConfigStatus, StudioConfig, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/lab/autoverify", tags=["lab-autoverify"])

AV_KIND = "terminology"
AV_KEY = "lab-autoverify"
AUTO_ACTOR = "system:auto-verify"


def _lab():
    """app.lab, imported lazily (app.lab imports this module at top
    level so its table registers before create_all — the W4
    precedent — so a top-level lab import here would cycle)."""
    from . import lab
    return lab


# ---------------------------------------------------------------------------
# Own table: one row per evaluation under a published rule set
# ---------------------------------------------------------------------------

class LabAutoverifyEvent(Base):
    """One autoverification evaluation (LAB-D7): the bench, the rule
    set version that judged, the decision (released | held), the full
    rule trace, and — for holds — the named reasons plus the human
    acknowledgement state. The autoverification-rate panel is computed
    from these rows; a hold is dispositioned by acknowledgement here
    while the report itself always proceeds through the unchanged
    manual verify/release endpoints (this row never mutates it)."""
    __tablename__ = "lab_autoverify_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                           index=True)
    order_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    bench: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    rule_set_version: Mapped[int | None] = mapped_column(Integer,
                                                         nullable=True)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)  # released | held
    trace: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    hold_reasons: Mapped[list] = mapped_column(JSON, nullable=False,
                                               default=list)
    instrument_id: Mapped[str | None] = mapped_column(String(80),
                                                      nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="recorded")  # recorded | acknowledged
    acknowledged_by: Mapped[str | None] = mapped_column(String(120),
                                                        nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    ack_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)


# ---------------------------------------------------------------------------
# Governed config: terminology/lab-autoverify (draft-seeded template)
# ---------------------------------------------------------------------------

def default_autoverify_payload() -> dict:
    """The seeded DRAFT template (never published by the seed): one
    disabled rule set for the chemistry bench illustrating every rule
    type with GOVERNED DEFAULT figures — the laboratory director
    sets real bounds per analyte/bench before publishing. A bench
    publishes (and enables) its own set through Studio; benches with
    no published enabled set never auto-release."""
    return {
        "name": "OpenCare autoverification rule sets (Lab Depth D7 — "
                "DRAFT template, seeded unpublished)",
        "system": "urn:opencare:lab-autoverify",
        "concepts": [
            {"code": "chemistry", "display": "Chemistry bench rule set",
             "active": True, "enabled": False, "holdOnCritical": True,
             "rules": [
                 {"id": "chem-glucose-amr", "type": "amr",
                  "analyteCode": "2345-7", "low": 20.0, "high": 600.0},
                 {"id": "chem-potassium-amr", "type": "amr",
                  "analyteCode": "2823-3", "low": 1.5, "high": 10.0},
                 {"id": "chem-interference", "type": "interference",
                  "flagCodes": ["HEM", "ICT", "LIP"]},
                 {"id": "chem-creatinine-delta", "type": "delta",
                  "analyteCode": "2160-0", "absolute": 0.3,
                  "windowHours": 72,
                  "condition": {"outsideReferenceOnly": True}},
                 {"id": "chem-absurd-potassium", "type": "absurd",
                  "analyteCode": "2823-3", "low": 0.5, "high": 14.0},
             ]},
            {"code": "haematology", "display": "Haematology bench rule set",
             "active": True, "enabled": False, "holdOnCritical": True,
             "rules": [
                 {"id": "haem-rule-of-three", "type": "cross-analyte",
                  "mode": "ratio", "codeA": "718-7", "codeB": "4544-3",
                  "factor": 3.0, "tolerancePct": 10.0},
                 {"id": "haem-hgb-amr", "type": "amr",
                  "analyteCode": "718-7", "low": 2.0, "high": 25.0},
                 {"id": "haem-interference", "type": "interference",
                  "flagCodes": ["CLOT", "HEM"]},
             ]},
        ],
    }


def ensure_lab_autoverify_defaults(session: Session) -> None:
    """Seed terminology/lab-autoverify as a DRAFT v1 template
    (insert-only) — deliberately NOT published: publishing a bench's
    rule set is the laboratory's governed go-live act, so a fresh
    deployment auto-releases nothing. Wired from app.main — see the
    W3 INTEGRATION NOTE."""
    if list_config_versions(session, AV_KIND, AV_KEY):
        return
    row = StudioConfig(kind=AV_KIND, key=AV_KEY, version=1,
                       status=ConfigStatus.draft,
                       payload=default_autoverify_payload(),
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{AV_KIND}/{AV_KEY}@v1",
                 payload={"kind": AV_KIND, "key": AV_KEY, "version": 1,
                          "seeded_default": True, "seeded_as": "draft"})
    session.commit()


def _published_payload(session: Session) -> tuple[dict | None, int | None]:
    cfg = published_config(session, AV_KIND, AV_KEY)
    if cfg is None:
        return None, None
    return cfg.payload or {}, cfg.version


def rule_set_for(session: Session, bench: str | None) -> tuple[dict | None, int | None]:
    """The bench's rule-set concept from the PUBLISHED config, or
    (None, version): no published config, no concept for the bench,
    or an inactive concept all mean the bench has no rule set."""
    payload, version = _published_payload(session)
    if payload is None or not bench:
        return None, version
    for concept in payload.get("concepts") or []:
        if isinstance(concept, dict) and concept.get("code") == bench \
                and concept.get("active") is not False:
            return concept, version
    return None, version


def bench_rule_status(session: Session, bench: str) -> dict:
    """Published-or-not status for one bench (the web surface's
    rule-set column): unpublished / published-disabled / live."""
    concept, version = rule_set_for(session, bench)
    if concept is None:
        return {"bench": bench, "state": "unpublished",
                "rule_set_version": version, "rule_count": 0,
                "enabled": False}
    enabled = concept.get("enabled") is True
    return {"bench": bench,
            "state": "live" if enabled else "published-disabled",
            "rule_set_version": version,
            "rule_count": len(concept.get("rules") or []),
            "enabled": enabled,
            "hold_on_critical": concept.get("holdOnCritical") is not False}


# ---------------------------------------------------------------------------
# The ONE evaluation function (auto path + dry-run parity endpoint)
# ---------------------------------------------------------------------------

def _trace(rule_id, rule_type, outcome, reason, **extra):
    entry = {"rule_id": rule_id, "type": rule_type, "outcome": outcome,
             "reason": reason}
    entry.update(extra)
    return entry


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def evaluate_autoverification(session: Session, order, checked: list[dict],
                              instrument_id: str | None = None) -> dict:
    """Evaluate one created result set against its bench's published
    rule set. Returns {applicable, bench, rule_set_version, decision,
    trace, hold_reasons, rule_ids}.

    applicable=False (no published enabled set for the bench) means
    autoverification does not judge at all — the caller falls back to
    the legacy Phase 6B verify-only path / human verification.

    When applicable, EVERY standing gate and EVERY configured rule is
    evaluated and traced, even after a hold fires — the trace is the
    complete judgement, not the first failure. decision is "release"
    only when nothing held."""
    bench = _lab()._order_bench(session, order)
    concept, version = rule_set_for(session, bench)
    if concept is None or concept.get("enabled") is not True:
        return {"applicable": False, "bench": bench,
                "rule_set_version": version, "decision": None,
                "trace": [], "hold_reasons": [], "rule_ids": []}
    trace: list[dict] = []
    holds: list[str] = []

    def hold(rule_id, rule_type, reason, **extra):
        trace.append(_trace(rule_id, rule_type, "hold", reason, **extra))
        holds.append(f"{rule_id}: {reason}")

    def passed(rule_id, rule_type, reason="within governed bounds", **extra):
        trace.append(_trace(rule_id, rule_type, "pass", reason, **extra))

    def skipped(rule_id, rule_type, reason, **extra):
        trace.append(_trace(rule_id, rule_type, "skip", reason, **extra))

    codes = [c["concept"]["code"] for c in checked]
    by_code = {c["concept"]["code"]: c for c in checked}
    demographics = _lab()._demographics_for_order(session, order)
    subject_ref = (order.body or {}).get("subject", {}).get("reference")

    # Standing gate 1: the report-level QC gate — the identical
    # qc_blockers the human verify/release endpoints enforce.
    blockers = _lab().qc_blockers(session, codes)
    if blockers:
        hold("qc-gate", "qc",
             "QC gate blocks: " + ", ".join(
                 f"{b['analyte_code']} ({b['reason']})" for b in blockers),
             analytes=[b["analyte_code"] for b in blockers])
    else:
        passed("qc-gate", "qc", "no QC blocker for the resulted analytes")

    # Standing gate 2: the W4 instrument-path lock, when the results
    # name an instrument (failed latest QC / calibration lock on that
    # instrument + analyte). Never bypassed by autoverification.
    if instrument_id:
        from . import lab_qc
        path_blockers = lab_qc.instrument_path_blockers(
            session, instrument_id, codes)
        if path_blockers:
            hold("instrument-path", "instrument-path",
                 "instrument path locked: " + "; ".join(
                     b["detail"] for b in path_blockers),
                 instrument_id=instrument_id)
        else:
            passed("instrument-path", "instrument-path",
                   "instrument path open", instrument_id=instrument_id)
    else:
        skipped("instrument-path", "instrument-path",
                "results name no instrument — no instrument path to lock")

    # Standing gate 3: qualitative results are never auto-released
    # under this taxonomy (coded results take the human path).
    qualitative = [c for c in checked if c.get("qualitative")]
    if qualitative:
        hold("qualitative-guard", "qualitative",
             "qualitative (coded) results are not auto-released: "
             + ", ".join(c["concept"]["code"] for c in qualitative))
    else:
        passed("qualitative-guard", "qualitative",
               "all results numeric")

    # Standing gate 4: critical-value posture, governed per bench
    # (holdOnCritical defaults ON — absent means hold).
    hold_on_critical = concept.get("holdOnCritical") is not False
    criticals = []
    for entry in checked:
        if entry.get("qualitative"):
            continue
        interp = _lab().interpret_value(entry["value"], entry["concept"],
                                         demographics)
        if _lab()._is_critical(interp):
            criticals.append(entry["concept"]["code"])
    if criticals and hold_on_critical:
        hold("critical-posture", "critical",
             "critical value(s) held for human review under this "
             "bench's published holdOnCritical posture: "
             + ", ".join(criticals), analytes=criticals)
    elif criticals:
        passed("critical-posture", "critical",
               "critical value(s) present and this bench's published "
               "posture autoverifies them when every other rule passes: "
               + ", ".join(criticals), analytes=criticals)
    else:
        passed("critical-posture", "critical", "no critical values")

    # Configured rules.
    rules = concept.get("rules")
    if not isinstance(rules, list) or not rules:
        hold("rule-set", "rule-set",
             "the published rule set carries no rules — an empty set "
             "releases nothing (fail closed)")
        rules = []
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("id") \
                or not rule.get("type"):
            hold("rule-set", "rule-set",
                 f"malformed rule in the published set: {rule!r} — "
                 "fail closed")
            continue
        rid, rtype = str(rule["id"]), str(rule["type"])
        if rtype in ("amr", "absurd"):
            code = rule.get("analyteCode")
            entry = by_code.get(code)
            low, high = _num(rule.get("low")), _num(rule.get("high"))
            if entry is None:
                skipped(rid, rtype, f"analyte {code} not in this result set",
                        analyte_code=code)
                continue
            if entry.get("qualitative") or low is None and high is None:
                hold(rid, rtype,
                     f"rule for {code} is not evaluable (qualitative "
                     "result or no bounds published) — fail closed",
                     analyte_code=code)
                continue
            value = entry["value"]
            if (low is not None and value < low) \
                    or (high is not None and value > high):
                label = ("outside the analytical measurement range "
                         f"(AMR {low}–{high})" if rtype == "amr" else
                         "physiologically absurd value "
                         f"(absurd bounds {low}–{high})")
                hold(rid, rtype, f"{code} value {value} is {label}",
                     analyte_code=code, value=value, low=low, high=high)
            else:
                passed(rid, rtype, f"{code} value {entry['value']} inside "
                                   f"{rtype} bounds {low}–{high}",
                       analyte_code=code, value=entry["value"])
        elif rtype == "interference":
            flag_codes = [str(f) for f in (rule.get("flagCodes") or [])]
            code = rule.get("analyteCode")
            targets = [c for c in checked
                       if code is None or c["concept"]["code"] == code]
            fired = [(c["concept"]["code"], f) for c in targets
                     for f in (c.get("flags") or []) if f in flag_codes]
            if fired:
                hold(rid, rtype,
                     "analyser interference flag(s): " + ", ".join(
                         f"{c} flag {f}" for c, f in fired),
                     flags=[f for _c, f in fired])
            else:
                passed(rid, rtype,
                       "no interference flag from the governed list "
                       f"{flag_codes} on the evaluated results")
        elif rtype == "delta":
            code = rule.get("analyteCode")
            entry = by_code.get(code)
            if entry is None:
                skipped(rid, rtype, f"analyte {code} not in this result set",
                        analyte_code=code)
                continue
            if entry.get("qualitative"):
                hold(rid, rtype, f"delta rule on qualitative analyte "
                                  f"{code} is not evaluable — fail closed",
                     analyte_code=code)
                continue
            condition = rule.get("condition") or {}
            if condition.get("outsideReferenceOnly") is True:
                interp = _lab().interpret_value(
                    entry["value"], entry["concept"], demographics)
                if interp in (None, "N"):
                    skipped(rid, rtype,
                            "conditional delta: the current value is "
                            "inside its reference range, so the rule's "
                            "condition (outsideReferenceOnly) is not met "
                            "and no delta comparison runs",
                            analyte_code=code)
                    continue
            # The delta comparison itself is app.lab._delta_flag —
            # the identical function the manual verification queue
            # uses (CAP GEN.43890 parity), with the rule's governed
            # thresholds substituted for the catalog delta config.
            concept_copy = dict(entry["concept"])
            delta_cfg = {}
            if _num(rule.get("absolute")) is not None:
                delta_cfg["absolute"] = float(rule["absolute"])
            if _num(rule.get("pct")) is not None:
                delta_cfg["pct"] = float(rule["pct"])
            if _num(rule.get("windowHours")) is not None:
                delta_cfg["windowHours"] = float(rule["windowHours"])
            if not delta_cfg:
                hold(rid, rtype, f"delta rule for {code} publishes no "
                                 "absolute/pct threshold — fail closed",
                     analyte_code=code)
                continue
            concept_copy["delta"] = delta_cfg
            flag = _lab()._delta_flag(session, subject_ref, concept_copy,
                                       entry["value"])
            if flag is not None:
                hold(rid, rtype,
                     f"delta check failed for {code}: previous "
                     f"{flag['previous_value']} -> current "
                     f"{flag['current_value']} (change {flag['change']})",
                     analyte_code=code, previous=flag["previous_value"],
                     current=flag["current_value"])
            else:
                passed(rid, rtype,
                       f"delta check passed for {code} (no prior released "
                       "baseline inside the window, or change within the "
                       "governed thresholds)", analyte_code=code)
        elif rtype == "cross-analyte":
            mode = rule.get("mode")
            a, b = by_code.get(rule.get("codeA")), by_code.get(rule.get("codeB"))
            if a is None or b is None:
                skipped(rid, rtype,
                        "cross-analyte trap needs both analytes in the "
                        f"result set ({rule.get('codeA')}, {rule.get('codeB')})",
                        analyte_code=rule.get("codeA"))
                continue
            if a.get("qualitative") or b.get("qualitative"):
                hold(rid, rtype, "cross-analyte trap on a qualitative "
                                  "result is not evaluable — fail closed")
                continue
            va, vb = a["value"], b["value"]
            if mode == "ratio":
                factor, tol = _num(rule.get("factor")), _num(rule.get("tolerancePct"))
                if factor is None or tol is None or va == 0:
                    hold(rid, rtype, "ratio trap lacks factor/tolerance "
                                      "or its base value is zero — fail closed")
                    continue
                expected = factor * va
                deviation = abs(vb - expected) / abs(expected) * 100.0
                if deviation > tol:
                    hold(rid, rtype,
                         f"cross-analyte trap: {rule.get('codeB')} {vb} "
                         f"deviates {deviation:.1f}% from {factor} x "
                         f"{rule.get('codeA')} {va} (expected ~{expected:g}, "
                         f"tolerance {tol}%)",
                         value_a=va, value_b=vb, expected=expected)
                else:
                    passed(rid, rtype,
                           f"{rule.get('codeB')} {vb} is consistent with "
                           f"{factor} x {rule.get('codeA')} {va} "
                           f"(deviation {deviation:.1f}% <= {tol}%)",
                           value_a=va, value_b=vb)
            elif mode == "lte":
                if va > vb:
                    hold(rid, rtype,
                         f"cross-analyte trap: {rule.get('codeA')} {va} "
                         f"exceeds {rule.get('codeB')} {vb} "
                         "(the governed relation is codeA <= codeB)",
                         value_a=va, value_b=vb)
                else:
                    passed(rid, rtype,
                           f"{rule.get('codeA')} {va} <= {rule.get('codeB')} "
                           f"{vb} as governed", value_a=va, value_b=vb)
            else:
                hold(rid, rtype, f"unknown cross-analyte mode '{mode}' "
                                 "— fail closed")
        else:
            hold(rid, rtype, f"unknown rule type '{rtype}' in the "
                             "published set — fail closed")
    return {"applicable": True, "bench": bench,
            "rule_set_version": version,
            "decision": "hold" if holds else "release",
            "trace": trace, "hold_reasons": holds,
            "rule_ids": [t["rule_id"] for t in trace]}


# ---------------------------------------------------------------------------
# Recording + the release mutation
# ---------------------------------------------------------------------------

def _record_event(session: Session, report, order, evaluation: dict,
                  instrument_id: str | None) -> LabAutoverifyEvent:
    event = LabAutoverifyEvent(
        report_id=report.resource_id, order_id=order.resource_id,
        bench=evaluation["bench"] or "",
        rule_set_version=evaluation.get("rule_set_version"),
        decision="released" if evaluation["decision"] == "release" else "held",
        trace=evaluation["trace"], hold_reasons=evaluation["hold_reasons"],
        instrument_id=instrument_id,
        status="recorded")
    session.add(event)
    session.flush()
    return event


def record_hold(session: Session, report, order, evaluation: dict,
                instrument_id: str | None = None) -> LabAutoverifyEvent:
    """Record a HOLD event (the draft itself is untouched — it stays
    in the verification queue for the manual path). Audited."""
    event = _record_event(session, report, order, evaluation, instrument_id)
    append_audit(session, actor=AUTO_ACTOR, action="lab.autoverify.hold",
                 entity=f"DiagnosticReport/{report.resource_id}",
                 payload={"order_id": order.resource_id,
                          "bench": evaluation["bench"],
                          "rule_set_version": evaluation.get("rule_set_version"),
                          "hold_reasons": evaluation["hold_reasons"],
                          "event_id": event.id})
    session.commit()
    return event


def release_and_record(session: Session, report, order, evaluation: dict,
                       instrument_id: str | None = None) -> bool:
    """Auto-RELEASE under a published rule set: the Phase 6B verify
    mutation (report -> final, order -> verified, audited as
    system:auto-verify) followed by the SAME _release_now mutation
    the human release endpoint uses (observations final, visibility
    tag removed, order/specimen released, outbound ORU + rules
    engine fired in-transaction). The QC gate was evaluated as part
    of the rule trace — a blocked set never reaches here."""
    event = _record_event(session, report, order, evaluation, instrument_id)
    _lab()._set_status(report, "final")
    _lab()._set_status(order, "verified")
    specimen = _lab()._specimen_row(session, order)
    if specimen is not None:
        _lab()._set_status(specimen, "verified")
    session.flush()
    append_audit(session, actor=AUTO_ACTOR, action="lab.report.verify",
                 entity=f"DiagnosticReport/{report.resource_id}",
                 payload={"order_id": order.resource_id,
                          "auto_verified": True, "auto_released": True,
                          "bench": evaluation["bench"],
                          "rule_set_version": evaluation.get("rule_set_version"),
                          "rule_ids": evaluation["rule_ids"],
                          "event_id": event.id})
    system_user = SimpleNamespace(username=AUTO_ACTOR)
    _lab()._release_now(session, system_user, report, order,
                         extra_payload={
                             "auto_released": True,
                             "autoverify_event_id": event.id,
                             "bench": evaluation["bench"],
                             "rule_set_version":
                                 evaluation.get("rule_set_version")})
    session.commit()
    return True


# ---------------------------------------------------------------------------
# HTTP helpers + endpoints
# ---------------------------------------------------------------------------

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


def _event_out(event: LabAutoverifyEvent) -> dict:
    return {"id": event.id, "report_id": event.report_id,
            "order_id": event.order_id, "bench": event.bench,
            "rule_set_version": event.rule_set_version,
            "decision": event.decision, "trace": list(event.trace or []),
            "hold_reasons": list(event.hold_reasons or []),
            "instrument_id": event.instrument_id, "status": event.status,
            "acknowledged_by": event.acknowledged_by,
            "acknowledged_at": event.acknowledged_at.isoformat()
            if event.acknowledged_at else None,
            "ack_note": event.ack_note,
            "created_at": event.created_at.isoformat()
            if event.created_at else None}


def _known_benches(session: Session) -> list[str]:
    """Bench codes from the published bench registry (W1's
    terminology/lab-benches), falling back to the catalog's bench
    routing values when the registry is unavailable."""
    benches: list[str] = []
    cfg = published_config(session, "terminology", "lab-benches")
    if cfg is not None:
        for concept in (cfg.payload or {}).get("concepts") or []:
            if isinstance(concept, dict) and concept.get("code") \
                    and concept.get("active") is not False:
                benches.append(str(concept["code"]))
    if not benches:
        seen = set()
        for concept in _lab()._catalog(session).get("concepts") or []:
            bench = concept.get("bench") if isinstance(concept, dict) else None
            if bench and bench not in seen:
                seen.add(bench)
                benches.append(bench)
    return benches


@router.get("/status")
def av_status(session: Session = Depends(get_db),
              user: User = Depends(require_capability("lab.read"))):
    """Rule-set status per bench: unpublished (nothing auto-releases
    on that bench), published-disabled, or live — with the published
    config's version and the Studio surface that governs it."""
    payload, version = _published_payload(session)
    draft_exists = bool(list_config_versions(session, AV_KIND, AV_KEY))
    return {
        "config": {"kind": AV_KIND, "key": AV_KEY,
                   "published_version": version,
                   "draft_template_seeded": draft_exists,
                   "publish_in": ("System Studio -> Terminology -> "
                                  "lab-autoverify")},
        "benches": [bench_rule_status(session, b)
                    for b in _known_benches(session)],
        "legacy_catalog_autoverify": {
            "enabled": (_lab()._catalog(session).get("autoVerify") or {})
            .get("enabled") is True,
            "note": ("The Phase 6B catalog autoVerify path still "
                     "applies (verify-only, never releases) on benches "
                     "without a live D7 rule set.")},
    }


@router.post("/evaluate")
async def evaluate_dry_run(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.read"))):
    """Dry-run the ONE evaluation function against a hypothetical
    result set for an existing order — the manual-path parity surface:
    what autoverification WOULD decide, with the full rule trace,
    writing nothing. CAP GEN.43890: the delta rule inside runs
    app.lab._delta_flag, the same function the verification queue
    uses for human review."""
    payload = await _json_body(request)
    order_id = payload.get("order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        raise HTTPException(status_code=422, detail="'order_id' is required")
    order = _lab()._order_row(session, order_id.strip())
    checked = _lab()._validate_results(session, order,
                                        payload.get("results"))
    # Flags ride the dry-run exactly as they ride real entry.
    raw = payload.get("results") or []
    for entry, item in zip(checked, raw):
        flags = item.get("flags") if isinstance(item, dict) else None
        entry["flags"] = [str(f) for f in flags] if isinstance(flags, list) else []
    instrument_id = payload.get("instrument_id")
    evaluation = evaluate_autoverification(
        session, order, checked,
        instrument_id=str(instrument_id) if instrument_id else None)
    return {"order_id": order.resource_id, "dry_run": True,
            "writes": "none — evaluation only",
            **evaluation}


@router.get("/holds")
def list_holds(bench: str | None = Query(default=None),
               status: str | None = Query(default=None),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability("lab.read"))):
    """The hold queue: evaluations that held, newest first, each with
    its named reasons and full rule trace."""
    stmt = select(LabAutoverifyEvent).where(
        LabAutoverifyEvent.decision == "held").order_by(
        LabAutoverifyEvent.id.desc())
    if bench:
        stmt = stmt.where(LabAutoverifyEvent.bench == bench)
    if status:
        stmt = stmt.where(LabAutoverifyEvent.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_event_out(r) for r in rows], "total": len(rows)}


@router.get("/events")
def list_events(bench: str | None = Query(default=None),
                session: Session = Depends(get_db),
                user: User = Depends(require_capability("lab.read"))):
    """Every recorded evaluation (releases and holds), newest first."""
    stmt = select(LabAutoverifyEvent).order_by(LabAutoverifyEvent.id.desc())
    if bench:
        stmt = stmt.where(LabAutoverifyEvent.bench == bench)
    rows = session.scalars(stmt.limit(500)).all()
    return {"items": [_event_out(r) for r in rows], "total": len(rows)}


@router.post("/holds/{event_id}/acknowledge")
async def acknowledge_hold(event_id: int, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.verify"))):
    """Acknowledge a hold: a named reviewer records that the held
    draft was reviewed and will proceed through the manual
    verify/release endpoints (which stay the only human path — this
    acknowledgement never verifies or releases anything itself)."""
    payload = await _json_body(request)
    event = session.scalars(select(LabAutoverifyEvent).where(
        LabAutoverifyEvent.id == event_id)).first()
    if event is None or event.decision != "held":
        raise HTTPException(status_code=404,
                            detail=f"Autoverification hold {event_id} not found")
    if event.status == "acknowledged":
        raise HTTPException(
            status_code=409,
            detail=(f"Hold {event_id} was already acknowledged by "
                    f"'{event.acknowledged_by}'"))
    note = payload.get("note")
    event.status = "acknowledged"
    event.acknowledged_by = user.username
    event.acknowledged_at = datetime.now(timezone.utc)
    event.ack_note = note if isinstance(note, str) else None
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.autoverify.hold.acknowledge",
                 entity=f"DiagnosticReport/{event.report_id}",
                 payload={"event_id": event.id, "order_id": event.order_id,
                          "note": event.ack_note})
    session.commit()
    return _event_out(event)


@router.get("/rates")
def av_rates(days: int = Query(default=30, ge=1, le=365),
             session: Session = Depends(get_db),
             user: User = Depends(require_capability("lab.read"))):
    """Autoverification rate per bench over the window: released /
    held / total and the rate mature labs manage by. Benches with no
    evaluations in the window report zeroes, never a fake rate."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.scalars(select(LabAutoverifyEvent)).all()
    per_bench: dict[str, dict] = {b: {"bench": b, "released": 0, "held": 0}
                                  for b in _known_benches(session)}
    for row in rows:
        created = row.created_at
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                continue
        bucket = per_bench.setdefault(
            row.bench, {"bench": row.bench, "released": 0, "held": 0})
        if row.decision == "released":
            bucket["released"] += 1
        else:
            bucket["held"] += 1
    items = []
    for bucket in per_bench.values():
        total = bucket["released"] + bucket["held"]
        items.append({**bucket, "total": total,
                      "rate": (round(bucket["released"] / total, 4)
                               if total else None)})
    return {"window_days": days, "items": items,
            "generated_at": datetime.now(timezone.utc).isoformat()}
