"""Rules / automation engine — Phase 10, Track B.

Rules are governed Studio configuration (kind=rule): a published rule
binds a named domain EVENT to a server-side ACTION when a structured
CONDITION over the event's signals holds. Conditions are data —
{signal, op, value} leaves combined with all/any — evaluated here,
server-side. There is deliberately no expression language and no code
execution: events, signals and actions are all server-defined
registries (app.configstore.RULE_EVENTS / RULE_ACTIONS), and a config
can only bind them. This is the Track B clinical-safety rule: maximum
flexibility, bounded blast radius.

Emission points (v1):
- app.lab._release_now emits `lab.report.released`
  {critical, report_id, order_id, patient_ref} on every release. The
  seeded `critical-value-notification` rule re-expresses the Phase 2
  critical-value escalation through the engine: when a released report
  is critical, the rule OPENS the notification record (state pending)
  — the worklist item the notify/acknowledge workflow then works.
  Before Phase 10 that pending state was purely derived; the record
  now exists because a rule fired, and the firing is audited.
- app.inpatient._record_event emits `inpatient.census.changed`
  {event_type, occupied, total_beds, occupancy_pct, patient_ref} on
  every ADT event. The seeded `census-threshold-alert` rule proves
  generality: at >= 90% occupancy it opens a capacity-review Task.
- app.people's credential scan emits `workforce.credential.expiring`
  {days_to_expiry, staff_id, staff_name, credential_type,
  credential_id, expires_on} once per credential per expiry date for
  credentials inside the published alert window; the seeded
  `credential-expiry-alert` rule turns the urgent ones (<= 30 days)
  into renewal Tasks.

Every firing writes a hash-chained audit row (action `rules.fired`)
naming the rule key + published version, the event and the action
taken, in the same transaction as the action itself.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir
from .audit import append_audit
from .configstore import RULE_ACTIONS, RULE_EVENTS, published_configs
from .models import (ConfigStatus, FhirResource, LabCriticalNotification,
                     StudioConfig, User)
from .security import get_db, require_capability

router = APIRouter(prefix="/rules", tags=["rules"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Condition evaluation — pure, total: a malformed signal never raises,
# it simply does not match (a rule that cannot evaluate must not fire,
# and must never break the clinical write that emitted the event).
# ---------------------------------------------------------------------------

def _leaf_matches(cond: dict, signals: dict) -> bool:
    value = signals.get(cond.get("signal"))
    op = cond.get("op")
    target = cond.get("value")
    try:
        if op == "eq":
            return value == target
        if op == "ne":
            return value != target
        if op == "in":
            return isinstance(target, list) and value in target
        if op == "contains":
            return isinstance(value, (list, str)) and target in value
        if op in ("gt", "gte", "lt", "lte"):
            if isinstance(value, bool) or isinstance(target, bool):
                return False
            if not isinstance(value, (int, float)) \
                    or not isinstance(target, (int, float)):
                return False
            return {"gt": value > target, "gte": value >= target,
                    "lt": value < target, "lte": value <= target}[op]
    except Exception:
        return False
    return False


def evaluate_condition(cond, signals: dict) -> bool:
    if not isinstance(cond, dict):
        return False
    if "all" in cond:
        return all(evaluate_condition(c, signals) for c in cond["all"])
    if "any" in cond:
        return any(evaluate_condition(c, signals) for c in cond["any"])
    return _leaf_matches(cond, signals)


# ---------------------------------------------------------------------------
# Actions — server code, parameterised by the rule's action.params.
# Each returns a small detail dict recorded on the firing's audit row.
# ---------------------------------------------------------------------------

def _act_open_critical_notification(session: Session, signals: dict,
                                    params: dict) -> dict:
    report_id = signals.get("report_id")
    if not report_id:
        return {"skipped": "no report_id signal"}
    existing = session.scalars(
        select(LabCriticalNotification).where(
            LabCriticalNotification.report_id == report_id)).first()
    if existing is not None:
        return {"notification": "already-open", "report_id": report_id,
                "state": existing.state}
    record = LabCriticalNotification(report_id=report_id,
                                     order_id=signals.get("order_id") or "")
    session.add(record)
    session.flush()
    return {"notification": "opened", "report_id": report_id, "state": "pending"}


def _act_create_task(session: Session, signals: dict, params: dict) -> dict:
    task_id = str(uuid.uuid4())
    body = {
        "resourceType": "Task",
        "id": task_id,
        "status": "requested",
        "intent": "order",
        "description": params.get("title"),
        "authoredOn": _now_iso(),
        "requester": {"display": "rules-engine"},
    }
    if params.get("ownerRole"):
        body["owner"] = {"display": str(params["ownerRole"])}
    if signals.get("patient_ref"):
        body["for"] = {"reference": signals["patient_ref"]}
    row = FhirResource(resource_type="Task", resource_id=task_id,
                       version=1, body=body)
    fhir._apply_projections(row, "Task", body, task_id)
    session.add(row)
    session.flush()
    return {"task_id": task_id, "title": params.get("title")}


def _act_audit_alert(session: Session, signals: dict, params: dict,
                     actor: str, rule_key: str) -> dict:
    append_audit(session, actor=actor, action="rules.alert",
                 entity=f"rule:{rule_key}",
                 payload={"note": params.get("note"), "signals": signals})
    return {"alert": params.get("note")}


_ACTION_IMPLS = {
    "open_critical_notification": _act_open_critical_notification,
    "create_task": _act_create_task,
}


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

def evaluate_rules(session: Session, event: str, signals: dict,
                   actor: str = "system") -> list[dict]:
    """Evaluate every PUBLISHED, enabled rule bound to `event` against
    `signals`; execute the actions of those whose condition holds, in
    config-key order. Returns the firings (also audit-chained). Called
    inside the emitting module's transaction — the caller commits."""
    firings: list[dict] = []
    for row in published_configs(session, "rule"):
        payload = row.payload or {}
        if payload.get("event") != event:
            continue
        if payload.get("enabled") is False:
            continue
        if not evaluate_condition(payload.get("condition"), signals):
            continue
        action = payload.get("action") or {}
        action_type = action.get("type")
        params = action.get("params") or {}
        if action_type == "audit_alert":
            detail = _act_audit_alert(session, signals, params, actor, row.key)
        elif action_type in _ACTION_IMPLS:
            detail = _ACTION_IMPLS[action_type](session, signals, params)
        else:  # registry-validated at draft time; unreachable in practice
            detail = {"skipped": f"unknown action {action_type}"}
        append_audit(session, actor=actor, action="rules.fired",
                     entity=f"rule:{row.key}@v{row.version}",
                     payload={"rule": row.key, "version": row.version,
                              "event": event, "action": action_type,
                              "detail": detail})
        firings.append({"rule": row.key, "version": row.version,
                        "event": event, "action": action_type,
                        "detail": detail})
    session.flush()
    return firings


# ---------------------------------------------------------------------------
# API: registry introspection + manual evaluation (operations/testing)
# ---------------------------------------------------------------------------

@router.get("/registry")
def registry(session: Session = Depends(get_db),
             user: User = Depends(require_capability("studio.read"))):
    """The server-defined registries a rule config may bind: events
    (with their signals) and action types. The Studio UI renders its
    rule help from this, so the UI can never offer what the engine
    cannot do."""
    return {
        "events": {
            "lab.report.released": ["critical", "report_id", "order_id",
                                    "patient_ref"],
            "inpatient.census.changed": ["event_type", "occupied",
                                         "total_beds", "occupancy_pct",
                                         "patient_ref"],
            "pharmacy.dur.screened": ["crcl_ml_min", "medication_code",
                                      "order_id", "patient_ref"],
            "workforce.credential.expiring": ["days_to_expiry", "staff_id",
                                              "staff_name", "credential_type",
                                              "credential_id", "expires_on"],
            "device.temperature.excursion": ["device_key", "device_name",
                                             "location", "metric", "value",
                                             "unit", "min", "max",
                                             "owner_role"],
            "device.vitals.breach": ["code", "value", "breach",
                                     "patient_ref", "device_key"],
            "rfid.zone.mismatch": ["mismatch", "epc", "device_key",
                                   "entity_ref", "expected_zone",
                                   "observed_zone", "reader_id"],
            "radiology.critical.overdue": ["overdue", "report_id",
                                           "study_id", "patient_ref",
                                           "ordering_clinician",
                                           "minutes_overdue"],
            "inbox.critical.unacknowledged": ["critical", "result_id",
                                              "kind", "patient_ref",
                                              "age_hours", "sla_hours"],
        },
        "actions": list(RULE_ACTIONS),
        "events_emitted": list(RULE_EVENTS),
    }


@router.post("/evaluate")
async def evaluate(request: Request, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("studio.manage"))):
    """Run the engine against a caller-supplied event + signals.
    An operations/testing surface (the same evaluation the domain
    hooks perform): actions really execute, firings really audit."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict) or body.get("event") not in RULE_EVENTS:
        raise HTTPException(
            status_code=422,
            detail=f"'event' must be one of {list(RULE_EVENTS)}")
    signals = body.get("signals")
    if signals is not None and not isinstance(signals, dict):
        raise HTTPException(status_code=422, detail="'signals' must be an object")
    firings = evaluate_rules(session, body["event"], signals or {},
                             actor=user.username)
    session.commit()
    return {"event": body["event"], "firings": firings,
            "fired": len(firings)}


# ---------------------------------------------------------------------------
# Seeded rules (published v1 at startup, through governance)
# ---------------------------------------------------------------------------

def default_rule_payloads() -> dict[str, dict]:
    return {
        "critical-value-notification": {
            "name": "Critical value notification",
            "description": ("When a lab report is released containing a "
                            "critical (LL/HH) value, open its critical-"
                            "result notification record (pending) — the "
                            "worklist item the read-back notification "
                            "workflow then completes."),
            "event": "lab.report.released",
            "condition": {"signal": "critical", "op": "eq", "value": True},
            "action": {"type": "open_critical_notification"},
            "enabled": True,
        },
        "census-threshold-alert": {
            "name": "Census threshold alert",
            "description": ("When hospital occupancy reaches 90% on any "
                            "ADT event, open a capacity-review Task for "
                            "the bed-management team."),
            "event": "inpatient.census.changed",
            "condition": {"signal": "occupancy_pct", "op": "gte", "value": 90},
            "action": {"type": "create_task",
                       "params": {"title": "Census at or above 90% — "
                                           "capacity review",
                                  "ownerRole": "bed-management"}},
            "enabled": True,
        },
        "renal-dose-review": {
            "name": "Renal dose review",
            "description": ("Phase 11 worked example: when a pharmacist "
                            "verifies a medication order and the patient's "
                            "estimated creatinine clearance (Cockcroft-"
                            "Gault, computed by app.pharmacy_ops from the "
                            "latest released creatinine + weight) is below "
                            "the configured threshold, open a renal-dose "
                            "review Task for the pharmacy team. The "
                            "threshold lives in this rule's condition — "
                            "a site changes it by publishing a new rule "
                            "version, never by editing code."),
            "event": "pharmacy.dur.screened",
            "condition": {"signal": "crcl_ml_min", "op": "lt", "value": 30},
            "action": {"type": "create_task",
                       "params": {"title": "Renal dose review — CrCl "
                                           "below threshold",
                                  "ownerRole": "pharmacist"}},
            "enabled": True,
        },
        "credential-expiry-alert": {
            "name": "Credential expiry alert",
            "description": ("Phase 13 (Track E): the workforce credential "
                            "scan (app.people) emits "
                            "workforce.credential.expiring for every "
                            "credential inside the configured alert window "
                            "that has not been alerted for its current "
                            "expiry date; when the days remaining are at "
                            "or below this rule's threshold (seeded 30, "
                            "matching the workforce config alert window), "
                            "open a renewal Task for the workforce team. "
                            "A site widens or narrows the alert by "
                            "publishing a new rule version — the window "
                            "is configuration, not code."),
            "event": "workforce.credential.expiring",
            "condition": {"signal": "days_to_expiry", "op": "lte", "value": 30},
            "action": {"type": "create_task",
                       "params": {"title": "Credential expiring — arrange "
                                           "renewal before lapse",
                                  "ownerRole": "workforce-admin"}},
            "enabled": True,
        },
        "fridge-temperature-excursion": {
            "name": "Storage temperature excursion",
            "description": ("Phase 14 (Track H, cold-chain pattern): "
                            "when a storage sensor reading crosses its "
                            "device type's published range, app.devices "
                            "emits device.temperature.excursion (on the "
                            "transition into excursion only); this rule "
                            "opens an excursion-investigation Task for "
                            "the owning team (pharmacy for the seeded "
                            "fridges). The range lives in the published "
                            "terminology/devices config — a site "
                            "changes it there, never in code."),
            "event": "device.temperature.excursion",
            "condition": {"signal": "metric", "op": "eq", "value": "temperature"},
            "action": {"type": "create_task",
                       "params": {"title": "Storage temperature excursion "
                                           "— investigate cold chain",
                                  "ownerRole": "pharmacy"}},
            "enabled": True,
        },
        "vitals-threshold-breach": {
            "name": "Vitals threshold breach",
            "description": ("Phase 14 (Track H): a device-ingested "
                            "vital-sign Observation outside the "
                            "published terminology/devices thresholds "
                            "emits device.vitals.breach; this rule "
                            "opens a review Task linked to the patient. "
                            "Thresholds are configuration; both low "
                            "and high breaches land here (the breach "
                            "direction rides in the event signals and "
                            "the firing's audit row)."),
            "event": "device.vitals.breach",
            "condition": {"signal": "breach", "op": "in",
                          "value": ["low", "high"]},
            "action": {"type": "create_task",
                       "params": {"title": "Device vitals outside "
                                           "configured thresholds — review",
                                  "ownerRole": "nurse"}},
            "enabled": True,
        },
    }


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    from .configstore import list_config_versions
    if list_config_versions(session, kind, key):
        return
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


def ensure_rule_defaults(session: Session) -> None:
    for key, payload in default_rule_payloads().items():
        _seed_published(session, "rule", key, payload)
    session.commit()
