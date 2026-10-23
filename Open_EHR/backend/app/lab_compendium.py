"""Lab Depth D1 + D2 — laboratory compendium & benches as
organisational units (app.lab_compendium).

D1 (compendium & directory machinery) lives mostly in app.lab: the
per-test directory attributes (performing section/site, method,
specimen requirements, availability, per-priority TAT targets with a
declared clock) and the age/sex-partitioned reference ranges are
fields ON the published lab catalogue (terminology/lab-catalog),
validated by app.configstore.validate_terminology and enforced /
selected by app.lab at order placement and result interpretation.
This module serves the composed directory read model over that
published catalogue.

D2 (bench as organisational unit) is owned here:

- The bench registry is governed Studio configuration —
  terminology/lab-benches, seeded published v1 by
  :func:`ensure_lab_compendium_defaults` and republished through
  the normal Studio draft -> publish flow (the app.lab_depth
  precedent: a module-owned terminology key whose extra concept
  fields are data for the owning module). A bench carries: code
  (matching the catalogue's `bench` routing value), name, section,
  location (an org-hierarchy code from terminology/locations),
  supervisor (a staff-directory id) and hours.
- Bench-scoped authorisation extends the EXISTING grant ledger
  (app.privileging / staff_privileges): a bench grant is a
  StaffPrivilege row with privilege_code ``lab.bench.<bench-code>``
  and service_line ``laboratory``, recorded and revoked through
  app.privileging.grant_privilege / revoke_privilege (same audit
  actions, same never-edited history). :func:`require_bench_grant`
  is the check: administrators pass (system administration);
  every other actor must hold an ACTIVE grant of that bench's code
  under one of their identities (username, linked account staff id,
  or staff-directory id). Refusals name the bench, the grant code
  and the actor.
- Bench surfaces: workload (open work counts by status for the
  bench's catalogue-routed orders) and shift handover (open items +
  handover notes). Handover notes are this module's own table;
  creating and acknowledging a note are real bench work actions
  gated by the bench grant.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import lab
from .audit import append_audit
from .configstore import list_config_versions, published_config
from .db import Base
from .models import (ConfigStatus, FhirResource, Role, StaffMember,
                     StaffPrivilege, StudioConfig, User, utcnow)
from .privileging import active_grants, grant_privilege, revoke_privilege
from .security import get_db, require_capability, require_roles

router = APIRouter(prefix="/lab/compendium", tags=["lab-compendium"])

BENCHES_KIND = "terminology"
BENCHES_KEY = "lab-benches"
BENCH_SERVICE_LINE = "laboratory"
# Order statuses that close a piece of bench work.
TERMINAL_ORDER_STATUSES = ("released", "cancelled", "rejected", "completed")


# ---------------------------------------------------------------------------
# Own table: bench shift-handover notes
# ---------------------------------------------------------------------------

class LabBenchHandoverNote(Base):
    """One shift-handover note for one bench: free text left by the
    outgoing shift, acknowledged by the incoming shift. The note is
    the record — acknowledgement is a state change on the row,
    never an edit of the text."""
    __tablename__ = "lab_bench_handover_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    bench_code: Mapped[str] = mapped_column(String(80), nullable=False,
                                            index=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author: Mapped[str] = mapped_column(String(120), nullable=False,
                                        default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="open")
    acknowledged_by: Mapped[str | None] = mapped_column(String(120),
                                                        nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Bench registry (governed Studio config: terminology/lab-benches)
# ---------------------------------------------------------------------------

def _bench_entry(code, name, section, supervisor, days, opens, closes):
    return {"code": code, "display": name, "active": True,
            "section": section, "location": "facility-main",
            "supervisorStaffId": supervisor,
            "hours": {"days": days, "opensAt": opens, "closesAt": closes}}


def default_benches_payload() -> dict:
    """Seed bench registry: one bench per catalogue `bench` routing
    value. Location is the published org hierarchy's facility code;
    supervisor ids are staff-directory ids (synthetic seed data —
    a site republishes with its real supervisors)."""
    every_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    weekdays = ["mon", "tue", "wed", "thu", "fri"]
    core = "Core laboratory"
    specialist = "Specialist laboratory"
    concepts = [
        _bench_entry("chemistry", "Chemistry bench", core,
                     "staff-201", every_day, "00:00", "23:59"),
        _bench_entry("haematology", "Haematology bench", core,
                     "staff-202", every_day, "00:00", "23:59"),
        _bench_entry("coagulation", "Coagulation bench", core,
                     "staff-201", every_day, "00:00", "23:59"),
        _bench_entry("urinalysis", "Urinalysis bench", core,
                     "staff-202", every_day, "00:00", "23:59"),
        _bench_entry("microbiology", "Microbiology bench", specialist,
                     "staff-203", every_day, "00:00", "23:59"),
        _bench_entry("blood-bank", "Blood bank bench",
                     "Transfusion laboratory",
                     "staff-204", every_day, "00:00", "23:59"),
        _bench_entry("pathology", "Anatomic pathology bench",
                     "Anatomic pathology",
                     "staff-205", weekdays, "08:00", "17:00"),
        _bench_entry("virology", "Virology bench", specialist,
                     "staff-206", every_day, "00:00", "23:59"),
        _bench_entry("serology", "Serology / immunology bench", specialist,
                     "staff-206", every_day, "00:00", "23:59"),
        _bench_entry("toxicology", "Toxicology bench", specialist,
                     "staff-207", every_day, "00:00", "23:59"),
        _bench_entry("sendout", "Send-out bench", "Referral laboratory",
                     "staff-207", weekdays, "08:00", "16:00"),
    ]
    return {"name": "OpenCare laboratory benches",
            "system": "urn:opencare:lab-benches",
            "concepts": concepts}


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


def ensure_lab_compendium_defaults(session: Session) -> None:
    """Seed the bench registry as published Studio config v1
    (idempotent), the app.lab / app.lab_depth seeding pattern."""
    _seed_published(session, BENCHES_KIND, BENCHES_KEY,
                    default_benches_payload())
    session.commit()


def _benches_config(session: Session) -> dict:
    cfg = published_config(session, BENCHES_KIND, BENCHES_KEY)
    if cfg is None:
        raise HTTPException(
            status_code=503,
            detail=(f"No published bench registry "
                    f"({BENCHES_KIND}/{BENCHES_KEY}); publish one in "
                    "Studio before using bench surfaces"))
    return cfg.payload or {}


def _bench(session: Session, bench_code: str) -> dict:
    for concept in _benches_config(session).get("concepts") or []:
        if isinstance(concept, dict) and concept.get("code") == bench_code \
                and concept.get("active") is not False:
            return concept
    raise HTTPException(
        status_code=404,
        detail=(f"Bench '{bench_code}' is not in the published bench "
                f"registry ({BENCHES_KIND}/{BENCHES_KEY})"))


def _bench_out(session: Session, bench: dict, user: User) -> dict:
    code = bench.get("code")
    grants = _active_bench_grants(session, code)
    return {
        "code": code, "name": bench.get("display"),
        "section": bench.get("section"),
        "location": bench.get("location"),
        "supervisor_staff_id": bench.get("supervisorStaffId"),
        "hours": bench.get("hours"),
        "grant_code": bench_grant_code(code),
        "caller_has_grant": _actor_has_bench_grant(session, user, code),
        "grants": [
            {"grant_id": g.grant_id, "staff_id": g.staff_id,
             "granted_by": g.granted_by,
             "granted_at": g.granted_at.isoformat() if g.granted_at
             else None,
             "note": g.note}
            for g in grants],
    }


# ---------------------------------------------------------------------------
# Bench-scoped authorisation (extends app.privileging's grant ledger)
# ---------------------------------------------------------------------------

def bench_grant_code(bench_code: str) -> str:
    """The privilege code a bench grant carries on the shared
    staff_privileges ledger: ``lab.bench.<bench-code>``."""
    return f"lab.bench.{bench_code}"


def _actor_staff_ids(session: Session, user: User) -> set[str]:
    """Every identity a grant may be recorded under for this actor:
    the username, the linked account's staff id, and any
    staff-directory row carrying this username."""
    ids = lab._operator_ids(session, user)
    for staff in session.scalars(select(StaffMember).where(
            StaffMember.username == user.username)).all():
        if staff.staff_id:
            ids.add(staff.staff_id)
    return ids


def _active_bench_grants(session: Session, bench_code: str) -> list:
    code = bench_grant_code(bench_code)
    rows = []
    for staff_id in _grant_holder_ids(session, code):
        rows.extend(g for g in active_grants(session, staff_id)
                    if g.privilege_code == code)
    return rows


def _grant_holder_ids(session: Session, code: str) -> list[str]:
    return list(session.scalars(
        select(StaffPrivilege.staff_id).where(
            StaffPrivilege.privilege_code == code,
            StaffPrivilege.status == "active").distinct()).all())


def _actor_has_bench_grant(session: Session, user: User,
                           bench_code: str) -> bool:
    if user.role == Role.admin:
        return True
    code = bench_grant_code(bench_code)
    for staff_id in _actor_staff_ids(session, user):
        if any(g.privilege_code == code
               for g in active_grants(session, staff_id)):
            return True
    return False


def require_bench_grant(session: Session, user: User,
                        bench_code: str) -> None:
    """The bench-work gate (Lab Depth D2): the actor must hold an
    ACTIVE ``lab.bench.<bench_code>`` grant on the privileging
    ledger under one of their identities. Administrators pass.
    A refusal names the bench, the missing grant and the actor —
    a global lab.work capability is NOT a bench grant."""
    if _actor_has_bench_grant(session, user, bench_code):
        return
    raise HTTPException(
        status_code=403,
        detail=(f"Bench grant required: '{user.username}' holds no "
                f"active '{bench_grant_code(bench_code)}' grant for "
                f"bench '{bench_code}'; bench work on '{bench_code}' "
                "requires a grant recorded by a bench supervisor or "
                "administrator"))


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


def _bench_orders(session: Session, bench_code: str) -> list[FhirResource]:
    rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "ServiceRequest")
        .order_by(FhirResource.created_at.asc(),
                  FhirResource.id.asc())).all()
    return [row for row in rows
            if lab._order_bench(session, row) == bench_code]


def _order_summary(row: FhirResource) -> dict:
    body = row.body or {}
    return {"order_id": row.resource_id, "status": body.get("status"),
            "code": lab._code_of(body),
            "patient_ref": (body.get("subject") or {}).get("reference"),
            "priority": body.get("priority"),
            "authored_on": body.get("authoredOn")}


def _workload(session: Session, bench_code: str) -> dict:
    counts: dict[str, int] = {}
    open_items = []
    for row in _bench_orders(session, bench_code):
        status = (row.body or {}).get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
        if status not in TERMINAL_ORDER_STATUSES:
            open_items.append(_order_summary(row))
    return {"bench": bench_code, "counts_by_status": counts,
            "open_total": len(open_items), "open_items": open_items}


def _note_out(note: LabBenchHandoverNote) -> dict:
    return {"note_id": note.note_id, "bench_code": note.bench_code,
            "note": note.note, "author": note.author,
            "status": note.status,
            "acknowledged_by": note.acknowledged_by,
            "acknowledged_at": note.acknowledged_at.isoformat()
            if note.acknowledged_at else None,
            "created_at": note.created_at.isoformat()
            if note.created_at else None}


# ---------------------------------------------------------------------------
# D1 — directory read model over the published catalogue
# ---------------------------------------------------------------------------

def _directory_entry(concept: dict) -> dict:
    return {
        "code": concept.get("code"), "display": concept.get("display"),
        "active": concept.get("active", True),
        "bench": concept.get("bench"),
        "panel": bool(concept.get("panel")),
        "components": list(concept.get("components") or []),
        "unit": concept.get("unit"),
        "qualitative": bool(concept.get("qualitative")),
        "values": concept.get("values"),
        "reference_range": concept.get("referenceRange"),
        "reference_range_partitions":
            concept.get("referenceRangePartitions"),
        "directory": concept.get("directory"),
    }


@router.get("/directory")
def directory(session: Session = Depends(get_db),
              user: User = Depends(require_capability("lab.read"))):
    """The test compendium: every published catalogue concept with
    its D1 directory attributes (performing section/site, method,
    specimen requirements, availability, TAT targets with declared
    clocks) and partitioned reference ranges, as published."""
    catalog = lab._catalog(session)
    cfg = published_config(session, "terminology", lab.LAB_CATALOG_KEY)
    concepts = [c for c in catalog.get("concepts") or []
                if isinstance(c, dict)]
    return {
        "catalog_version": cfg.version if cfg else None,
        "config": {"kind": "terminology", "key": lab.LAB_CATALOG_KEY,
                   "publish_in": ("System Studio -> Terminology -> "
                                  "lab-catalog")},
        "tests": [_directory_entry(c) for c in concepts],
        "total": len(concepts),
    }


@router.get("/directory/{code}")
def directory_test(code: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    concept = lab._concept(session, code)
    if concept is None:
        raise HTTPException(
            status_code=404,
            detail=f"Lab code '{code}' is not in the published lab catalog")
    entry = _directory_entry(concept)
    if concept.get("panel"):
        entry["component_entries"] = [
            _directory_entry(lab._concept(session, comp) or {"code": comp})
            for comp in concept.get("components") or []]
    return entry


# ---------------------------------------------------------------------------
# D2 — benches, grants, workload, handover
# ---------------------------------------------------------------------------

@router.get("/benches")
def list_benches(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.read"))):
    payload = _benches_config(session)
    cfg = published_config(session, BENCHES_KIND, BENCHES_KEY)
    benches = [c for c in payload.get("concepts") or []
               if isinstance(c, dict) and c.get("active") is not False]
    return {
        "registry_version": cfg.version if cfg else None,
        "config": {"kind": BENCHES_KIND, "key": BENCHES_KEY,
                   "publish_in": ("System Studio -> Terminology -> "
                                  "lab-benches")},
        "benches": [_bench_out(session, b, user) for b in benches],
        "total": len(benches),
    }


@router.post("/benches/{bench_code}/grants", status_code=201)
async def grant_bench(bench_code: str, request: Request,
                      session: Session = Depends(get_db),
                      user: User = Depends(require_roles(Role.admin,
                                                         Role.configurator))):
    """Record a bench grant on the shared privileging ledger
    (privilege_code lab.bench.<bench>, service_line laboratory).
    Grant administration is an admin/configurator act; the grant
    itself is what bench work checks."""
    _bench(session, bench_code)
    body = await _json_body(request)
    staff_id = body.get("staff_id")
    if not isinstance(staff_id, str) or not staff_id.strip():
        raise HTTPException(status_code=422,
                            detail="'staff_id' (the grant holder) is "
                                   "required")
    note = body.get("note")
    row = grant_privilege(session, user.username, staff_id.strip(),
                          bench_grant_code(bench_code),
                          service_line=BENCH_SERVICE_LINE,
                          note=note if isinstance(note, str) else "")
    session.commit()
    return {"grant_id": row.grant_id, "staff_id": row.staff_id,
            "privilege": row.privilege_code,
            "service_line": row.service_line, "status": row.status,
            "granted_by": row.granted_by,
            "granted_at": row.granted_at.isoformat() if row.granted_at
            else None}


@router.post("/benches/{bench_code}/grants/{grant_id}/revoke")
async def revoke_bench_grant(bench_code: str, grant_id: str,
                             request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(
                                 require_roles(Role.admin,
                                               Role.configurator))):
    """Revoke a bench grant (a state change on the ledger row; the
    grant history is preserved). The grant must belong to THIS
    bench."""
    _bench(session, bench_code)
    body = await _json_body(request)
    row = session.scalars(select(StaffPrivilege).where(
        StaffPrivilege.grant_id == grant_id)).first()
    if row is None or row.privilege_code != bench_grant_code(bench_code):
        raise HTTPException(
            status_code=404,
            detail=f"Bench grant '{grant_id}' not found for bench "
                   f"'{bench_code}'")
    reason = body.get("reason")
    row = revoke_privilege(session, user.username, grant_id,
                           reason if isinstance(reason, str) else "")
    session.commit()
    return {"grant_id": row.grant_id, "staff_id": row.staff_id,
            "privilege": row.privilege_code, "status": row.status,
            "revoked_by": row.revoked_by,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at
            else None}


@router.get("/benches/{bench_code}/workload")
def bench_workload(bench_code: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    """Open work counts by status for one bench. Bench-scoped:
    the caller must hold this bench's grant (administrators pass)."""
    _bench(session, bench_code)
    require_bench_grant(session, user, bench_code)
    return _workload(session, bench_code)


@router.get("/benches/{bench_code}/handover")
def bench_handover(bench_code: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    """The shift-handover list for one bench: the open work items
    plus every handover note (open first). Bench-grant gated, like
    the workload view."""
    _bench(session, bench_code)
    require_bench_grant(session, user, bench_code)
    notes = session.scalars(
        select(LabBenchHandoverNote).where(
            LabBenchHandoverNote.bench_code == bench_code)
        .order_by(LabBenchHandoverNote.id.desc())).all()
    workload = _workload(session, bench_code)
    return {"bench": bench_code,
            "open_items": workload["open_items"],
            "open_total": workload["open_total"],
            "counts_by_status": workload["counts_by_status"],
            "notes": [_note_out(n) for n in notes],
            "open_notes": sum(1 for n in notes if n.status == "open")}


@router.post("/benches/{bench_code}/handover", status_code=201)
async def create_handover_note(bench_code: str, request: Request,
                               session: Session = Depends(get_db),
                               user: User = Depends(
                                   require_capability("lab.work"))):
    """Leave a shift-handover note on a bench — a real bench work
    action, gated by THIS bench's grant (a global lab.work
    capability alone does not suffice)."""
    _bench(session, bench_code)
    require_bench_grant(session, user, bench_code)
    body = await _json_body(request)
    note_text = body.get("note")
    if not isinstance(note_text, str) or not note_text.strip():
        raise HTTPException(status_code=422,
                            detail="'note' (the handover text) is required")
    note = LabBenchHandoverNote(
        note_id=f"handover-{uuid.uuid4().hex[:10]}",
        bench_code=bench_code, note=note_text.strip(),
        author=user.username, status="open")
    session.add(note)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.compendium.handover.create",
                 entity=f"LabBench/{bench_code}",
                 payload={"note_id": note.note_id, "bench": bench_code})
    session.commit()
    return _note_out(note)


@router.post("/benches/{bench_code}/handover/{note_id}/acknowledge")
async def acknowledge_handover_note(bench_code: str, note_id: str,
                                    request: Request,
                                    session: Session = Depends(get_db),
                                    user: User = Depends(
                                        require_capability("lab.work"))):
    """Acknowledge a handover note (the incoming shift's bench work
    act). Bench-grant gated; an already-acknowledged note refuses
    with 409 — acknowledgement is recorded once, by name."""
    _bench(session, bench_code)
    require_bench_grant(session, user, bench_code)
    note = session.scalars(select(LabBenchHandoverNote).where(
        LabBenchHandoverNote.note_id == note_id,
        LabBenchHandoverNote.bench_code == bench_code)).first()
    if note is None:
        raise HTTPException(
            status_code=404,
            detail=f"Handover note '{note_id}' not found on bench "
                   f"'{bench_code}'")
    if note.status == "acknowledged":
        raise HTTPException(
            status_code=409,
            detail=(f"Handover note '{note_id}' on bench "
                    f"'{bench_code}' was already acknowledged by "
                    f"'{note.acknowledged_by}'"))
    note.status = "acknowledged"
    note.acknowledged_by = user.username
    note.acknowledged_at = datetime.now(timezone.utc)
    session.flush()
    append_audit(session, actor=user.username,
                 action="lab.compendium.handover.acknowledge",
                 entity=f"LabBench/{bench_code}",
                 payload={"note_id": note.note_id, "bench": bench_code})
    session.commit()
    return _note_out(note)
