"""Lab Depth D10 + D11 — TAT targets & escalation, the lab-side
critical-value clock, and same-specimen reflex depth (app.lab_tat).

D10 — TAT targets & escalation
------------------------------
The governed per-test / per-priority TAT targets are W1's directory
data: `directory.tatTargets` on each published lab-catalogue concept
(terminology/lab-catalog), each target carrying a DECLARED clock —
"order-to-result" or "receipt-to-result". This module CONSUMES those
targets; there is deliberately no second target store. A test with
no target for an order's priority is simply UNMONITORED, and the
worklist says so by name.

Every TAT figure this module produces DECLARES its clock: the two
clocks are computed and labelled separately and never blended into
one number (research §1.4: TAT has no single definition — the CAP
Q-Probes finding). For a live (not yet released) order the clock
runs from its declared start (order authoredOn, or the specimen's
receivedDateTime) to now; the board entry names start, target, due
time, elapsed minutes and the fraction of target consumed.

Live worklists are per bench (the catalogue `bench` routing value):
approaching-breach (elapsed >= the governed warning fraction of the
target) and breached (elapsed > target). Escalation follows the
chain bench -> supervisor -> lab manager: a governed number of
minutes past breach, a scan fires an escalation RECORD (this
module's own table) plus a notification Task to the next level —
the bench supervisor named in the governed bench registry
(terminology/lab-benches, app.lab_compendium) at level 1, the
laboratory manager named in this module's governed config at
level 2. Each firing is audited; a (subject, level) pair fires
ONCE, however often the scan runs (the results-inbox SLA pattern:
an audit-marked, idempotent scan — patterns reused, not its clocks).

The critical-value clock lives INSIDE the lab: a critical result
that is verified/released but NOT yet notified starts a governed
timer at release (the report's `issued` stamp). While the existing
notification machinery (app.lab: pending -> notified ->
acknowledged, read-back enforced) has not recorded notified_to +
read_back, the chase list shows the elapsed time against the
governed notify window; past the window the same escalation chain
fires (bench supervisor, then lab manager) until notification
lands — the chase stops the moment it does. This module never
records a notification itself and never weakens read-back.

D11 — same-specimen reflex depth
--------------------------------
When a published reflex rule (terminology/lab-catalog `reflexRules`,
governed Studio config) fires, the add-on now runs against the
EXISTING specimen: the generated order links the source order's
specimen, the volume is checked and consumed through
app.lab_specimens' aliquot / add-on machinery (_required_volume,
_remaining_volume, record_addon_consumption -> LabAddonRecord +
addon_consumed_ml), and no fresh specimen is ever minted. Insufficient
volume is a NAMED REFUSAL recorded on the work (a LabReflexFiring
row with the refusal text, no order created), with a recollect path:
POST /lab/tat/reflex-firings/{id}/request-recollect opens a
recollect Task on the source order through the existing Task
machinery (the same shape app.lab's rejection flow creates).
Multi-step cascades (A -> B -> C) evaluate stepwise: resulting a
reflex order fires the next rule against the same specimen, each
step's trigger evidence (rule, trigger code/value/kind) recorded on
its firing row with its cascade step and parent firing. Triggers are
quantitative (below-low / above-high / critical against the
analyte's reference range, or below-value / above-value against a
rule-carried numeric threshold) and qualitative (qualitative-match
against a published coded value). Every generated order carries the
reflex-billing flag (body `reflexBilling: true` + the
urn:opencare:extension/reflex-billing extension).

A reflex order is created in `ordered` state (the Phase 6B shape)
already linked to its received specimen, so bench work begins with
POST /lab/tat/reflex-firings/{id}/begin, which moves it to
`received` (collection and receipt already happened — for THIS
specimen) and the normal result-entry path takes over.

Governed Studio configuration (configure, don't fork):
  terminology/lab-catalog   directory.tatTargets (W1, consumed)
                            + reflexRules (consumed; rules stay
                            governed Studio config)
  terminology/lab-benches   bench registry incl. supervisor (W1)
  terminology/lab-tat       THIS module: warning fraction,
                            escalation minutes past breach per
                            level, lab-manager identity, and the
                            critical notify window. Seeded
                            published v1 by
                            ensure_lab_tat_defaults; every seeded
                            figure is labelled a governed default
                            for the laboratory director to set.

Storage: two NEW tables owned by this module (LabTatEscalation,
LabReflexFiring) — operational lab data on the LabQcRun /
LabCriticalNotification precedent. No existing table gains a
column, so no ensure hook is owed.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import (DateTime, Float, Integer, JSON, String, Text,
                        UniqueConstraint, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir, lab, lab_specimens
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import ConfigStatus, FhirResource, StudioConfig, User, utcnow
from .security import get_db, require_capability

router = APIRouter(prefix="/lab/tat", tags=["lab-tat"])

TAT_CONFIG_KEY = "lab-tat"
REFLEX_BILLING_EXTENSION = "urn:opencare:extension/reflex-billing"
REFLEX_SPECIMEN_EXTENSION = "urn:opencare:extension/reflex-specimen"
OPEN_ORDER_STATUSES = ("ordered", "collected", "received", "in-process",
                       "resulted", "verified")
# Specimen states from which a same-specimen reflex can never run.
UNUSABLE_SPECIMEN_STATUSES = ("rejected", "disposed", "unavailable")


# ---------------------------------------------------------------------------
# Own tables
# ---------------------------------------------------------------------------

class LabTatEscalation(Base):
    """One fired escalation: (kind, subject, level) fires once.

    kind is "tat" (subject = order id) or "critical" (subject =
    report id). The row IS the escalation record; the notification
    Task it opened is linked by task_id."""
    __tablename__ = "lab_tat_escalations"
    __table_args__ = (UniqueConstraint("kind", "subject_key", "level",
                                       name="uq_lab_tat_escalation_once"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    escalation_id: Mapped[str] = mapped_column(String(120), unique=True,
                                               nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    subject_key: Mapped[str] = mapped_column(String(120), nullable=False,
                                             index=True)
    order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bench_code: Mapped[str | None] = mapped_column(String(80), nullable=True,
                                                   index=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    level_name: Mapped[str] = mapped_column(String(40), nullable=False,
                                            default="")
    target_role: Mapped[str] = mapped_column(String(80), nullable=False,
                                             default="")
    target_staff_id: Mapped[str | None] = mapped_column(String(120),
                                                        nullable=True)
    target_display: Mapped[str | None] = mapped_column(String(200),
                                                       nullable=True)
    clock: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    task_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    fired_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                          default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class LabReflexFiring(Base):
    """One reflex-rule evaluation that matched its trigger — fired
    onto the existing specimen, or refused by name (insufficient
    volume / no usable specimen). The firing row is the evidence:
    rule, trigger value, specimen/aliquot consumed, cascade step."""
    __tablename__ = "lab_reflex_firings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    firing_id: Mapped[str] = mapped_column(String(120), unique=True,
                                            nullable=False, index=True)
    rule_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                         index=True)
    source_order_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                                 index=True)
    source_report_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                                  index=True)
    trigger_code: Mapped[str] = mapped_column(String(60), nullable=False)
    trigger_value: Mapped[str | None] = mapped_column(String(200),
                                                      nullable=True)
    trigger_kind: Mapped[str] = mapped_column(String(20), nullable=False,
                                              default="quantitative")
    add_code: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="fired", index=True)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    specimen_key: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True)
    specimen_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    required_volume_ml: Mapped[float | None] = mapped_column(Float,
                                                             nullable=True)
    remaining_before_ml: Mapped[float | None] = mapped_column(Float,
                                                              nullable=True)
    addon_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    addon_order_id: Mapped[str | None] = mapped_column(String(120),
                                                       nullable=True,
                                                       index=True)
    reflex_billing: Mapped[bool] = mapped_column(default=False,
                                                 nullable=False)
    cascade_step: Mapped[int] = mapped_column(Integer, nullable=False,
                                              default=1)
    parent_firing_id: Mapped[str | None] = mapped_column(String(120),
                                                          nullable=True)
    recollect_task_id: Mapped[str | None] = mapped_column(String(120),
                                                          nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Governed config (terminology/lab-tat)
# ---------------------------------------------------------------------------

def default_lab_tat_payload() -> dict:
    return {
        "name": "OpenCare laboratory TAT escalation & critical-value "
                "clock (Lab Depth D10/D11)",
        # `concepts` is non-empty so the payload validates through
        # the shared Studio terminology validator on republish.
        "concepts": [{"code": "lab-tat",
                      "display": "Laboratory TAT escalation & "
                                 "critical-value clock",
                      "active": True}],
        # GOVERNED DEFAULTS — the laboratory director sets the real
        # figures per site policy; nothing here is a universal
        # standard. The notify window's seed (30 min) follows the
        # institutional SOP pattern in the research (§1.4).
        "governedDefaultsLabel": (
            "GOVERNED DEFAULTS — laboratory director to set; NOT a "
            "universal standard."),
        "warningFraction": 0.8,
        "escalation": {
            "supervisorAfterBreachMinutes": 0,
            "managerAfterBreachMinutes": 60,
            "labManagerStaffId": "staff-101",
            "labManagerDisplay": "Laboratory manager (on-call)",
        },
        "criticalNotify": {
            "windowMinutes": 30,
            "supervisorAfterBreachMinutes": 0,
            "managerAfterBreachMinutes": 30,
        },
    }


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


def ensure_lab_tat_defaults(session: Session) -> None:
    """Seed terminology/lab-tat as published Studio config v1
    (idempotent), the app.lab_compendium seeding pattern."""
    _seed_published(session, "terminology", TAT_CONFIG_KEY,
                    default_lab_tat_payload())
    session.commit()


def _tat_config(session: Session) -> dict:
    cfg = published_config(session, "terminology", TAT_CONFIG_KEY)
    if cfg is not None and isinstance(cfg.payload, dict):
        return cfg.payload
    return default_lab_tat_payload()


def _num(cfg: dict, *path, default: float) -> float:
    node: object = cfg
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        return default
    return float(node)


def _warning_fraction(cfg: dict) -> float:
    value = _num(cfg, "warningFraction", default=0.8)
    return value if 0 < value < 1 else 0.8


def _bench_supervisor(session: Session, bench_code: str | None) -> dict:
    """The bench's supervisor from the governed bench registry
    (terminology/lab-benches, W1). Unknown bench -> a role-only
    target (the escalation still fires, addressed to the role)."""
    cfg = published_config(session, "terminology", "lab-benches")
    if cfg is not None and bench_code:
        for concept in (cfg.payload or {}).get("concepts") or []:
            if isinstance(concept, dict) \
                    and concept.get("code") == bench_code:
                return {"staff_id": concept.get("supervisorStaffId"),
                        "display": concept.get("display")}
    return {"staff_id": None, "display": None}


# ---------------------------------------------------------------------------
# Helpers
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


def _minutes_between(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / 60.0, 2)


def _escalation_out(row: LabTatEscalation) -> dict:
    return {
        "escalation_id": row.escalation_id, "kind": row.kind,
        "subject_key": row.subject_key, "order_id": row.order_id,
        "bench_code": row.bench_code, "level": row.level,
        "level_name": row.level_name, "target_role": row.target_role,
        "target_staff_id": row.target_staff_id,
        "target_display": row.target_display, "clock": row.clock,
        "detail": row.detail or {}, "task_id": row.task_id,
        "fired_by": row.fired_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _firing_out(row: LabReflexFiring) -> dict:
    return {
        "firing_id": row.firing_id, "rule_id": row.rule_id,
        "source_order_id": row.source_order_id,
        "source_report_id": row.source_report_id,
        "trigger_code": row.trigger_code,
        "trigger_value": row.trigger_value,
        "trigger_kind": row.trigger_kind, "add_code": row.add_code,
        "status": row.status, "refusal_reason": row.refusal_reason,
        "specimen_key": row.specimen_key, "specimen_id": row.specimen_id,
        "required_volume_ml": row.required_volume_ml,
        "remaining_before_ml": row.remaining_before_ml,
        "addon_record_id": row.addon_record_id,
        "addon_order_id": row.addon_order_id,
        "reflex_billing": bool(row.reflex_billing),
        "cascade_step": row.cascade_step,
        "parent_firing_id": row.parent_firing_id,
        "recollect_task_id": row.recollect_task_id,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ---------------------------------------------------------------------------
# D10 — TAT computation with DECLARED clocks
# ---------------------------------------------------------------------------

def _default_priority(session: Session) -> str:
    catalog = lab._catalog(session)
    collection = catalog.get("collection") or {}
    return collection.get("defaultPriority") or "routine"


def _order_clock_entries(session: Session, order: FhirResource,
                         now: datetime, warning_fraction: float) -> tuple:
    """(monitored entries, unmonitored entry or None) for one open
    order. One monitored entry PER DECLARED CLOCK target matching
    the order's priority — the clocks are never blended."""
    body = order.body or {}
    code = lab._code_of(body)
    concept = lab._concept(session, code) if code else None
    if concept is None:
        return [], None
    bench = concept.get("bench") or "unassigned"
    priority = body.get("priority") or _default_priority(session)
    base = {"order_id": order.resource_id, "code": code,
            "display": concept.get("display"), "bench": bench,
            "priority": priority, "order_status": body.get("status"),
            "patient_ref": (body.get("subject") or {}).get("reference"),
            "authored_on": body.get("authoredOn")}
    directory = concept.get("directory") or {}
    targets = [t for t in directory.get("tatTargets") or []
               if isinstance(t, dict) and t.get("priority") == priority]
    if not targets:
        reason = ("no TAT target published for this test in the lab "
                  "directory (terminology/lab-catalog directory."
                  "tatTargets)") if not directory.get("tatTargets") else (
                  f"directory TAT targets exist but none for priority "
                  f"'{priority}'")
        return [], {**base, "monitored": False, "reason": reason}
    specimen = lab._specimen_row(session, order)
    received = lab._parse_ts((specimen.body or {}).get("receivedDateTime")) \
        if specimen is not None else None
    authored = lab._parse_ts(body.get("authoredOn"))
    entries = []
    for target in targets:
        clock = target.get("clock")
        target_minutes = float(target.get("targetMinutes"))
        start = authored if clock == "order-to-result" else received
        entry = {**base, "monitored": True, "clock": clock,
                 "clock_label": ("order → result" if clock == "order-to-result"
                                 else "receipt → result"),
                 "target_minutes": target_minutes,
                 "start_at": start.isoformat() if start else None,
                 "due_at": (start + timedelta(minutes=target_minutes)
                            ).isoformat() if start else None,
                 "specimen_id": specimen.resource_id if specimen else None}
        if start is None:
            entry.update({"state": "awaiting-receipt", "elapsed_minutes": None,
                          "fraction": None, "minutes_past_breach": None})
        else:
            elapsed = _minutes_between(start, now)
            entry.update({
                "state": ("breached" if elapsed > target_minutes else
                          "approaching" if elapsed >=
                          warning_fraction * target_minutes else "within"),
                "elapsed_minutes": elapsed,
                "fraction": round(elapsed / target_minutes, 4)
                if target_minutes else None,
                "minutes_past_breach": round(elapsed - target_minutes, 2)
                if elapsed > target_minutes else 0.0})
        entries.append(entry)
    return entries, None


def compute_board(session: Session, now: datetime | None = None) -> dict:
    """The live TAT board: per-bench approaching / breached lists
    (each entry declaring its clock), the within-target count, and
    the unmonitored list — a test without a published target is
    named here, never silently dropped."""
    now = now or datetime.now(timezone.utc)
    cfg = _tat_config(session)
    fraction = _warning_fraction(cfg)
    orders = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "ServiceRequest")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    benches: dict[str, dict] = {}
    unmonitored = []
    for order in orders:
        if (order.body or {}).get("status") not in OPEN_ORDER_STATUSES:
            continue
        entries, unmon = _order_clock_entries(session, order, now, fraction)
        if unmon is not None:
            unmonitored.append(unmon)
        for entry in entries:
            bench = benches.setdefault(
                entry["bench"], {"bench": entry["bench"],
                                 "approaching": [], "breached": [],
                                 "awaiting_receipt": [],
                                 "within_count": 0})
            if entry["state"] == "breached":
                bench["breached"].append(entry)
            elif entry["state"] == "approaching":
                bench["approaching"].append(entry)
            elif entry["state"] == "awaiting-receipt":
                bench["awaiting_receipt"].append(entry)
            else:
                bench["within_count"] += 1
    return {"generated_at": now.isoformat(),
            "warning_fraction": fraction,
            "clocks": ["order-to-result", "receipt-to-result"],
            "benches": [benches[k] for k in sorted(benches)],
            "unmonitored": unmonitored,
            "unmonitored_count": len(unmonitored)}


# ---------------------------------------------------------------------------
# D10 — critical-value clock (inside the lab)
# ---------------------------------------------------------------------------

def compute_critical_chase(session: Session,
                           now: datetime | None = None) -> list[dict]:
    """Released critical reports with their notification chase
    state. The clock starts at RELEASE (report `issued`); it stops
    the moment app.lab's notification machinery records the
    notification (notified_to + read-back) — this list then shows
    the report as notified, with when."""
    now = now or datetime.now(timezone.utc)
    cfg = _tat_config(session)
    window = _num(cfg, "criticalNotify", "windowMinutes", default=30.0)
    reports = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "DiagnosticReport")
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    items = []
    for report in reports:
        order = lab._report_order(session, report)
        if order is None or (order.body or {}).get("status") != "released":
            continue
        if not lab._report_is_critical(session, report):
            continue
        released_at = lab._parse_ts((report.body or {}).get("issued"))
        record = lab._notification_record(session, report.resource_id)
        notified = record is not None \
            and record.state in ("notified", "acknowledged")
        elapsed = _minutes_between(released_at, now) \
            if released_at is not None else None
        bench = lab._order_bench(session, order) or "unassigned"
        items.append({
            "report_id": report.resource_id,
            "order_id": order.resource_id, "bench": bench,
            "code": lab._code_of(report.body or {}),
            "patient_ref": (report.body or {}).get("subject", {})
            .get("reference"),
            "released_at": released_at.isoformat() if released_at else None,
            "notify_window_minutes": window,
            "elapsed_minutes": elapsed,
            "minutes_past_window": (round(elapsed - window, 2)
                                    if elapsed is not None
                                    and elapsed > window else 0.0),
            "notification": lab._notification_out(session, report, True),
            "chase_state": ("notified" if notified else
                            "breached" if elapsed is not None
                            and elapsed > window else "within-window"),
        })
    return items


# ---------------------------------------------------------------------------
# D10 — escalation firing (bench -> supervisor -> lab manager)
# ---------------------------------------------------------------------------

def _fire_escalation(session: Session, user: User, *, kind: str,
                     subject_key: str, order_id: str | None,
                     bench_code: str | None, level: int,
                     clock: str | None, detail: dict,
                     patient_ref: str | None) -> LabTatEscalation | None:
    """Fire ONE escalation level for one subject, or None when that
    (kind, subject, level) already fired — one escalation per level
    per order/report, no duplicate spam. Creates the record, the
    notification Task to the level's target, and the audit row."""
    existing = session.scalars(
        select(LabTatEscalation).where(
            LabTatEscalation.kind == kind,
            LabTatEscalation.subject_key == subject_key,
            LabTatEscalation.level == level)).first()
    if existing is not None:
        return None
    cfg = _tat_config(session)
    if level == 1:
        supervisor = _bench_supervisor(session, bench_code)
        target_role = "bench-supervisor"
        target_staff_id = supervisor["staff_id"]
        target_display = (f"Supervisor of bench '{bench_code}'"
                          + (f" ({supervisor['display']})"
                             if supervisor["display"] else ""))
        level_name = "supervisor"
    else:
        target_role = "lab-manager"
        target_staff_id = (cfg.get("escalation") or {}).get(
            "labManagerStaffId")
        target_display = (cfg.get("escalation") or {}).get(
            "labManagerDisplay") or "Laboratory manager"
        level_name = "lab-manager"
    focus_type = "DiagnosticReport" if kind == "critical" \
        else "ServiceRequest"
    description = (
        f"{'Critical-value notification overdue' if kind == 'critical' else 'TAT breach'} "
        f"— escalation level {level} ({level_name}) for "
        f"{focus_type}/{subject_key}"
        + (f" on clock '{clock}'" if clock else "")
        + f": {detail.get('summary', '')}")
    task_body = {
        "resourceType": "Task", "id": str(uuid.uuid4()),
        "status": "requested", "intent": "order",
        "code": {"coding": [{"system": "urn:opencare:task-type",
                             "code": ("critical-value-escalation"
                                      if kind == "critical"
                                      else "lab-tat-escalation"),
                             "display": description[:120]}],
                 "text": "Lab escalation"},
        "focus": {"reference": f"{focus_type}/{subject_key}"},
        "authoredOn": lab._now_iso(),
        "requester": {"display": "system:lab-tat"},
        "owner": {"display": target_display},
        "description": description,
    }
    if patient_ref:
        task_body["for"] = {"reference": patient_ref}
    task = fhir.create_resource("Task", task_body, session, user)
    row = LabTatEscalation(
        escalation_id=f"esc-{uuid.uuid4().hex[:12]}", kind=kind,
        subject_key=subject_key, order_id=order_id, bench_code=bench_code,
        level=level, level_name=level_name, target_role=target_role,
        target_staff_id=target_staff_id, target_display=target_display,
        clock=clock, detail=detail, task_id=task["id"],
        fired_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action=("lab.critical.escalate" if kind == "critical"
                         else "lab.tat.escalate"),
                 entity=f"{focus_type}/{subject_key}",
                 payload={"escalation_id": row.escalation_id,
                          "level": level, "level_name": level_name,
                          "target_role": target_role,
                          "target_staff_id": target_staff_id,
                          "bench_code": bench_code, "clock": clock,
                          "task_id": task["id"], **detail})
    return row


def run_escalation_scan(session: Session, user: User,
                        now: datetime | None = None) -> dict:
    """One escalation scan (the results-inbox SLA-scan pattern on
    the lab's own clocks): every breached TAT clock entry and every
    un-notified critical past its window fires the levels whose
    governed minutes-past-breach have elapsed. Idempotent — a
    level that already fired never refires."""
    now = now or datetime.now(timezone.utc)
    cfg = _tat_config(session)
    tat_sup = _num(cfg, "escalation", "supervisorAfterBreachMinutes",
                   default=0.0)
    tat_mgr = _num(cfg, "escalation", "managerAfterBreachMinutes",
                   default=60.0)
    crit_sup = _num(cfg, "criticalNotify", "supervisorAfterBreachMinutes",
                    default=0.0)
    crit_mgr = _num(cfg, "criticalNotify", "managerAfterBreachMinutes",
                    default=30.0)
    fired: list[LabTatEscalation] = []

    board = compute_board(session, now)
    worst: dict[str, dict] = {}
    for bench in board["benches"]:
        for entry in bench["breached"]:
            current = worst.get(entry["order_id"])
            if current is None or entry["minutes_past_breach"] \
                    > current["minutes_past_breach"]:
                worst[entry["order_id"]] = entry
    for entry in worst.values():
        past = entry["minutes_past_breach"]
        detail = {"summary": (f"order {entry['order_id']} "
                              f"({entry['code']}) is {past} min past its "
                              f"{entry['target_minutes']} min target on the "
                              f"declared {entry['clock']} clock"),
                  "clock": entry["clock"],
                  "target_minutes": entry["target_minutes"],
                  "elapsed_minutes": entry["elapsed_minutes"],
                  "minutes_past_breach": past}
        for level, threshold in ((1, tat_sup), (2, tat_mgr)):
            if past >= threshold:
                row = _fire_escalation(
                    session, user, kind="tat",
                    subject_key=entry["order_id"],
                    order_id=entry["order_id"], bench_code=entry["bench"],
                    level=level, clock=entry["clock"], detail=detail,
                    patient_ref=entry.get("patient_ref"))
                if row is not None:
                    fired.append(row)

    for item in compute_critical_chase(session, now):
        if item["chase_state"] != "breached":
            continue
        past = item["minutes_past_window"]
        detail = {"summary": (f"critical report {item['report_id']} "
                              f"released {item['elapsed_minutes']} min ago "
                              f"is still un-notified "
                              f"({past} min past the "
                              f"{item['notify_window_minutes']} min window)"),
                  "elapsed_minutes": item["elapsed_minutes"],
                  "notify_window_minutes": item["notify_window_minutes"],
                  "minutes_past_window": past}
        for level, threshold in ((1, crit_sup), (2, crit_mgr)):
            if past >= threshold:
                row = _fire_escalation(
                    session, user, kind="critical",
                    subject_key=item["report_id"],
                    order_id=item["order_id"], bench_code=item["bench"],
                    level=level, clock=None, detail=detail,
                    patient_ref=item.get("patient_ref"))
                if row is not None:
                    fired.append(row)
    session.commit()
    return {"fired": [_escalation_out(r) for r in fired],
            "fired_count": len(fired), "scanned_at": now.isoformat()}


# ---------------------------------------------------------------------------
# D11 — same-specimen reflex engine
# ---------------------------------------------------------------------------

def _rule_fires(rule: dict, entry: dict, session: Session,
                order: FhirResource) -> bool:
    """Trigger evaluation for one created result against one rule.
    Quantitative: below-low / above-high / critical against the
    analyte's reference range, or below-value / above-value against
    the rule's own governed numeric threshold. Qualitative:
    qualitative-match against the rule's published matchValue."""
    when = rule.get("when")
    value = entry["value"]
    if entry["qualitative"]:
        return when == "qualitative-match" \
            and rule.get("matchValue") == value
    analyte = entry["concept"]
    rng = analyte.get("referenceRange") or {}
    if when == "below-low":
        return rng.get("low") is not None and value < rng["low"]
    if when == "above-high":
        return rng.get("high") is not None and value > rng["high"]
    if when == "critical":
        demographics = lab._demographics_for_order(session, order)
        return lab._is_critical(
            lab.interpret_value(value, analyte, demographics))
    if when in ("below-value", "above-value"):
        threshold = rule.get("value")
        if isinstance(threshold, bool) \
                or not isinstance(threshold, (int, float)):
            return False
        return value < float(threshold) if when == "below-value" \
            else value > float(threshold)
    return False


def _firing_for_order(session: Session,
                      order_id: str) -> LabReflexFiring | None:
    return session.scalars(
        select(LabReflexFiring).where(
            LabReflexFiring.addon_order_id == order_id,
            LabReflexFiring.status.in_(
                ("fired", "in-analysis")))).first()


def fire_reflex_same_specimen(session: Session, user: User,
                              order: FhirResource, report_id: str,
                              checked: list[dict]) -> list[str]:
    """LAB-D11: the reflex engine, same-specimen semantics. Called
    by app.lab._fire_reflex_rules (its owned region) after a draft
    report exists. For each created result matching a published
    reflex rule, the add-on order is created against the SOURCE
    order's EXISTING specimen — volume-checked and consumed through
    app.lab_specimens' add-on machinery — or refused BY NAME on a
    LabReflexFiring record (no fresh order, no fresh specimen, no
    silent skip). Deduped per (report, rule) by the firing record,
    with the Phase 6B order-level dedupe preserved."""
    rules = lab._catalog(session).get("reflexRules") or []
    if not rules:
        return []
    subject_ref = (order.body or {}).get("subject", {}).get("reference")
    created: list[str] = []
    fired_rules: set[str] = set()
    for entry in checked:
        analyte, value = entry["concept"], entry["value"]
        for rule in rules:
            if not isinstance(rule, dict) \
                    or rule.get("triggerCode") != analyte["code"] \
                    or rule.get("id") in fired_rules:
                continue
            if not _rule_fires(rule, entry, session, order):
                continue
            rule_id = rule.get("id")
            fired_rules.add(rule_id)
            # Dedupe: a firing already recorded for this report +
            # rule, or a legacy reflex order for this rule, stands.
            prior = session.scalars(
                select(LabReflexFiring).where(
                    LabReflexFiring.source_report_id == report_id,
                    LabReflexFiring.rule_id == rule_id)).first()
            if prior is not None:
                continue
            if lab._reflex_order_exists(session, subject_ref, report_id,
                                         rule_id):
                continue
            add_code = rule.get("addCode")
            target = lab._concept(session, add_code) if add_code else None
            if target is None or target.get("active") is False \
                    or target.get("panel"):
                continue
            parent = _firing_for_order(session, order.resource_id)
            firing = LabReflexFiring(
                firing_id=f"rfx-{uuid.uuid4().hex[:12]}",
                rule_id=rule_id, source_order_id=order.resource_id,
                source_report_id=report_id,
                trigger_code=analyte["code"],
                trigger_value=(value if isinstance(value, str)
                               else repr(value)),
                trigger_kind=("qualitative" if entry["qualitative"]
                              else "quantitative"),
                add_code=add_code, reflex_billing=True,
                cascade_step=(parent.cascade_step + 1 if parent else 1),
                parent_firing_id=parent.firing_id if parent else None,
                created_by="system:reflex")
            specimen_row = lab._specimen_row(session, order)
            if specimen_row is None:
                firing.status = "refused-no-specimen"
                firing.reflex_billing = False
                firing.refusal_reason = (
                    f"Reflex '{rule_id}' refused: source order "
                    f"{order.resource_id} has no specimen — a "
                    "same-specimen reflex cannot run without the "
                    "existing specimen, and no fresh order is minted")
                session.add(firing)
                session.flush()
                append_audit(
                    session, actor="system:reflex",
                    action="lab.reflex.refused",
                    entity=f"DiagnosticReport/{report_id}",
                    payload={"firing_id": firing.firing_id,
                             "rule_id": rule_id,
                             "reason": firing.refusal_reason})
                session.commit()
                continue
            node = lab_specimens._ensure_node(session, specimen_row)
            firing.specimen_key = node.specimen_key
            firing.specimen_id = specimen_row.resource_id
            specimen_status = (specimen_row.body or {}).get("status")
            if node.status in UNUSABLE_SPECIMEN_STATUSES \
                    or specimen_status == "rejected":
                firing.status = "refused-specimen-unavailable"
                firing.reflex_billing = False
                firing.refusal_reason = (
                    f"Reflex '{rule_id}' refused: specimen "
                    f"{node.specimen_key} is "
                    f"'{node.status or specimen_status}' — a "
                    "same-specimen reflex requires a usable specimen; "
                    "recollect through the recollect path")
                session.add(firing)
                session.flush()
                append_audit(
                    session, actor="system:reflex",
                    action="lab.reflex.refused",
                    entity=f"Specimen/{specimen_row.resource_id}",
                    payload={"firing_id": firing.firing_id,
                             "rule_id": rule_id,
                             "reason": firing.refusal_reason})
                session.commit()
                continue
            required = lab_specimens._required_volume(target)
            remaining = lab_specimens._remaining_volume(session, node)
            firing.required_volume_ml = required
            firing.remaining_before_ml = remaining
            if required > remaining + 1e-9:
                firing.status = "refused-insufficient-volume"
                firing.reflex_billing = False
                firing.refusal_reason = (
                    f"Insufficient volume for reflex '{rule_id}' "
                    f"add-on '{add_code}': requires {required} mL per "
                    f"its aliquot rules, specimen {node.specimen_key} "
                    f"has {remaining} mL remaining — no fresh order "
                    "minted; recollect via the reflex recollect path")
                session.add(firing)
                session.flush()
                append_audit(
                    session, actor="system:reflex",
                    action="lab.reflex.refused",
                    entity=f"Specimen/{specimen_row.resource_id}",
                    payload={"firing_id": firing.firing_id,
                             "rule_id": rule_id,
                             "add_code": add_code,
                             "required_volume_ml": required,
                             "remaining_volume_ml": remaining,
                             "reason": firing.refusal_reason})
                session.commit()
                continue
            order_body = {
                "resourceType": "ServiceRequest",
                "id": str(uuid.uuid4()),
                "status": "ordered", "intent": "order",
                "reflex": True, "reflexBilling": True,
                "category": [{"coding": [{
                    "system": "http://snomed.info/sct",
                    "code": "108252007",
                    "display": "Laboratory procedure"}]}],
                "code": {"coding": [{"system": lab.LOINC,
                                     "code": add_code,
                                     "display": target.get("display")}],
                         "text": target.get("display")},
                "subject": {"reference": subject_ref},
                "authoredOn": lab._now_iso(),
                "requester": {"display": f"system:reflex ({rule_id})"},
                "specimen": [{"reference":
                              f"Specimen/{specimen_row.resource_id}"}],
                "extension": [
                    {"url": lab.REFLEX_EXTENSION, "valueString": rule_id},
                    {"url": lab.REFLEX_SOURCE_EXTENSION,
                     "valueReference":
                     {"reference": f"DiagnosticReport/{report_id}"}},
                    {"url": REFLEX_BILLING_EXTENSION, "valueBoolean": True},
                    {"url": REFLEX_SPECIMEN_EXTENSION,
                     "valueString": node.specimen_key},
                ],
            }
            if (order.body or {}).get("priority"):
                order_body["priority"] = order.body["priority"]
            rendered = fhir.create_resource("ServiceRequest", order_body,
                                            session, user)
            record = lab_specimens.record_addon_consumption(
                session, node, target, rendered["id"],
                requested_by=f"system:reflex ({rule_id})")
            firing.status = "fired"
            firing.addon_order_id = rendered["id"]
            firing.addon_record_id = record.id
            session.add(firing)
            session.flush()
            append_audit(session, actor="system:reflex",
                         action="lab.reflex.create",
                         entity=f"ServiceRequest/{rendered['id']}",
                         payload={"rule_id": rule_id,
                                  "firing_id": firing.firing_id,
                                  "trigger_code": analyte["code"],
                                  "trigger_value": value,
                                  "trigger_kind": firing.trigger_kind,
                                  "source_report_id": report_id,
                                  "source_order_id": order.resource_id,
                                  "specimen_key": node.specimen_key,
                                  "required_volume_ml": required,
                                  "reflex_billing": True,
                                  "cascade_step": firing.cascade_step,
                                  "parent_firing_id":
                                  firing.parent_firing_id})
            session.commit()
            created.append(rendered["id"])
    return created


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/config")
def tat_config(session: Session = Depends(get_db),
               user: User = Depends(require_capability("lab.read"))):
    cfg = published_config(session, "terminology", TAT_CONFIG_KEY)
    payload = _tat_config(session)
    return {"config": {"kind": "terminology", "key": TAT_CONFIG_KEY,
                       "version": cfg.version if cfg else None,
                       "publish_in": ("System Studio -> Terminology -> "
                                      "lab-tat")},
            "warning_fraction": _warning_fraction(payload),
            "escalation": payload.get("escalation") or {},
            "critical_notify": payload.get("criticalNotify") or {},
            "governed_defaults_label": payload.get(
                "governedDefaultsLabel")}


@router.get("/board")
def tat_board(session: Session = Depends(get_db),
              user: User = Depends(require_capability("lab.read"))):
    """The live per-bench TAT board (D10): approaching-breach and
    breached worklists, every entry declaring its clock, plus the
    unmonitored list (tests with no published target for the
    order's priority — named, never hidden)."""
    return compute_board(session)


@router.get("/critical-chase")
def critical_chase(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    """The un-notified criticals chase list (D10): released critical
    reports, elapsed time since release against the governed notify
    window, and the notification state from app.lab's machinery —
    the chase stops the moment notification lands."""
    items = compute_critical_chase(session)
    return {"items": items, "total": len(items),
            "unnotified": sum(1 for i in items
                              if i["chase_state"] != "notified")}


@router.get("/escalations")
def escalation_log(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(
        select(LabTatEscalation).order_by(LabTatEscalation.id.asc())).all()
    return {"items": [_escalation_out(r) for r in rows],
            "total": len(rows)}


@router.post("/scan")
def escalation_scan(session: Session = Depends(get_db),
                    user: User = Depends(require_capability("lab.work"))):
    """Run one escalation scan: fire every due escalation level for
    breached TAT clocks and overdue critical notifications. Each
    (subject, level) fires once, however often the scan runs."""
    return run_escalation_scan(session, user)


@router.get("/reflex")
def reflex_activity(session: Session = Depends(get_db),
                    user: User = Depends(require_capability("lab.read"))):
    """Reflex activity (D11): every firing with its trigger
    evidence, the specimen/aliquot consumed, the billing flag, the
    cascade step — and every named refusal, visible as work."""
    rows = session.scalars(
        select(LabReflexFiring).order_by(LabReflexFiring.id.asc())).all()
    return {"items": [_firing_out(r) for r in rows], "total": len(rows),
            "refused": sum(1 for r in rows
                           if r.status.startswith("refused"))}


@router.post("/reflex-firings/{firing_id}/begin")
def begin_reflex_work(firing_id: str, session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.work"))):
    """Begin bench work on a fired reflex: the add-on order moves
    ordered -> received against its EXISTING specimen (collection
    and receipt already happened for this specimen — no new
    specimen is created), so the normal result-entry path takes
    over. This is also how a cascade advances: resulting the
    add-on fires the next rule against the same specimen."""
    firing = session.scalars(select(LabReflexFiring).where(
        LabReflexFiring.firing_id == firing_id)).first()
    if firing is None:
        raise HTTPException(status_code=404,
                            detail=f"Reflex firing '{firing_id}' not found")
    if firing.status != "fired" or not firing.addon_order_id:
        raise HTTPException(
            status_code=409,
            detail=(f"Reflex firing '{firing_id}' is "
                    f"'{firing.status}'; only a fired reflex with a "
                    "generated order can begin bench work"))
    order = lab._order_row(session, firing.addon_order_id)
    if (order.body or {}).get("status") != "ordered":
        raise HTTPException(
            status_code=409,
            detail=(f"Reflex order {firing.addon_order_id} is "
                    f"'{(order.body or {}).get('status')}'; bench work "
                    "begins from 'ordered'"))
    lab._set_status(order, "received")
    firing.status = "in-analysis"
    session.flush()
    append_audit(session, actor=user.username, action="lab.reflex.begin",
                 entity=f"ServiceRequest/{firing.addon_order_id}",
                 payload={"firing_id": firing_id,
                          "specimen_key": firing.specimen_key})
    session.commit()
    return {"firing": _firing_out(firing), "order": fhir._render(order)}


@router.post("/reflex-firings/{firing_id}/request-recollect",
             status_code=201)
async def request_reflex_recollect(
        firing_id: str, request: Request,
        session: Session = Depends(get_db),
        user: User = Depends(require_capability("lab.work"))):
    """The recollect path for a refused reflex (D11): opens a
    recollect Task on the SOURCE order through the existing Task
    machinery (the shape app.lab's rejection flow creates), naming
    the specimen and the refusal. One task per firing."""
    firing = session.scalars(select(LabReflexFiring).where(
        LabReflexFiring.firing_id == firing_id)).first()
    if firing is None:
        raise HTTPException(status_code=404,
                            detail=f"Reflex firing '{firing_id}' not found")
    if not firing.status.startswith("refused"):
        raise HTTPException(
            status_code=409,
            detail=(f"Reflex firing '{firing_id}' is "
                    f"'{firing.status}'; the recollect path exists "
                    "for refused reflexes"))
    if firing.recollect_task_id:
        raise HTTPException(
            status_code=409,
            detail=(f"Reflex firing '{firing_id}' already has a "
                    f"recollect task ({firing.recollect_task_id})"))
    try:
        payload = await _json_body(request)
    except HTTPException:
        payload = {}
    note = payload.get("note")
    source = lab._order_row(session, firing.source_order_id)
    subject_ref = (source.body or {}).get("subject", {}).get("reference")
    task = fhir.create_resource("Task", {
        "resourceType": "Task", "id": str(uuid.uuid4()),
        "status": "requested", "intent": "order",
        "code": {"coding": [{"system": "urn:opencare:task-type",
                             "code": "recollect",
                             "display": "Recollect specimen"}],
                 "text": "Recollect specimen"},
        "focus": {"reference":
                  f"ServiceRequest/{firing.source_order_id}"},
        "for": {"reference": subject_ref},
        "authoredOn": lab._now_iso(),
        "requester": {"display": user.username},
        "description": (
            f"Recollect specimen for reflex '{firing.rule_id}' "
            f"(add-on '{firing.add_code}') on specimen "
            f"{firing.specimen_key}: {firing.refusal_reason}"
            + (f" Note: {note}" if isinstance(note, str) and note else "")),
    }, session, user)
    firing.recollect_task_id = task["id"]
    firing.status = "recollect-requested"
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.reflex.recollect.request",
                 entity=f"ServiceRequest/{firing.source_order_id}",
                 payload={"firing_id": firing_id,
                          "rule_id": firing.rule_id,
                          "task_id": task["id"],
                          "specimen_key": firing.specimen_key})
    session.commit()
    return {"firing": _firing_out(firing), "task_id": task["id"]}
