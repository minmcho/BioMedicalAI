"""Phase 15 (B / BD-09 / BD-10 / BD-34) — laboratory depth.

- **Test Manager + Panel Manager** (inside-the-lab working surfaces
  over the GOVERNED catalogue, terminology/lab-catalog): enriched
  published views (aliquot/derivation rules, panel composition,
  reflex rules, specimen/container requirements) and draft-compose
  endpoints that write ONLY onto the DRAFT version of the catalogue
  through the Studio draft machinery pattern (app.protocols
  create/update-draft mirrored against kind=terminology,
  key=lab-catalog). Published payloads are NEVER mutated here;
  publishing stays in Studio (POST /studio/config/.../publish).
  Compose authorisation: capability lab.work AND (role
  admin/configurator OR the caller's linked staff job_role equals
  the laboratory-director role named in the published
  terminology/lab-governance config).
- **BD-09 blood-bank lookback + FDA deviation reporting**: a
  lookback case traced from a trigger unit over the Phase 6B
  blood-bank spine (units, issues, transfusion Procedures),
  quarantining implicated unissued units through the blood bank's
  own status value, with consignee/recipient notification records;
  case payloads state the >=10-year traceability retention of
  21 CFR 606.160. Deviation reports are generated as structured
  records + a PDF document via app.reports machinery.
- **BD-10 CLIA six-element competency**: the six CLIA competency
  elements as published config (terminology/lab-competency);
  per-person, per-test-system records with per-element assessment
  dates/assessors; currency computed from the configured window;
  overdue surfaced on a worklist shaped like the Phase 13
  workforce credential worklist.
- **BD-34 external EQA/PT exchange**: enrolments for external PT
  programmes; export/import in the provider format defined by
  published config (integration/eqa-provider: format name + field
  map) — fixture-defined and honestly labelled. Imported scores are
  filed onto the internal EQA machinery's records (LabEqaRecord,
  app.lab_benches) with server-computed pass/fail. LIVE provider
  exchange is NOT RUN.
"""
import copy
import csv
import io
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import DateTime, Float, Integer, JSON, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir, lab, lab_benches
from .audit import append_audit
from .configstore import (get_config_version, list_config_versions,
                          published_config, validate_payload,
                          ConfigPayloadError)
from .db import Base
from .models import (BloodUnit, ConfigStatus, FhirResource, LabEqaRecord,
                     StaffMember, StudioConfig, User, UserAccount, utcnow)
from .security import get_db, require_capability

router = APIRouter(prefix="/lab", tags=["lab_depth"])

GOVERNANCE_KEY = "lab-governance"
COMPETENCY_KEY = "lab-competency"
EQA_PROVIDER_KEY = "eqa-provider"
CATALOG_KIND = "terminology"
RETENTION_YEARS = 10
RETENTION_CITATION = "21 CFR 606.160"


# ---------------------------------------------------------------------------
# Own tables
# ---------------------------------------------------------------------------

class CliaCompetencyRecord(Base):
    """One CLIA competency record: one testing person x one test
    system, with per-element assessments ({element, assessed_on,
    assessor}) for the six published CLIA elements. Currency is
    COMPUTED at read from the published validity window — never a
    stored flag."""
    __tablename__ = "lab_clia_competency_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    staff_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                          index=True)
    test_system: Mapped[str] = mapped_column(String(60), nullable=False,
                                             index=True)
    elements: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    recorded_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class LookbackCase(Base):
    """One blood-bank lookback case (BD-09): opened from a trigger
    unit/test result, carrying the complete component->recipient
    trace and the >=10-year traceability statement."""
    __tablename__ = "lab_lookback_cases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(120), unique=True,
                                         nullable=False, index=True)
    trigger_unit_id: Mapped[str] = mapped_column(String(80), nullable=False,
                                                 index=True)
    trigger_detail: Mapped[str] = mapped_column(Text, nullable=False,
                                                default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="open")
    trace: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class LookbackNotification(Base):
    """One consignee/recipient notification record for a lookback
    case, with its own state (pending -> notified -> acknowledged)."""
    __tablename__ = "lab_lookback_notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(120), nullable=False,
                                         index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    recipient_ref: Mapped[str] = mapped_column(String(200), nullable=False,
                                               default="")
    unit_id: Mapped[str] = mapped_column(String(80), nullable=False,
                                         default="")
    state: Mapped[str] = mapped_column(String(40), nullable=False,
                                       default="pending")
    notified_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class DeviationReport(Base):
    """One biological-product deviation report record (BD-09): the
    structured artefact plus PDF-document generation state."""
    __tablename__ = "lab_deviation_reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[str] = mapped_column(String(120), unique=True,
                                           nullable=False, index=True)
    case_id: Mapped[str | None] = mapped_column(String(120), nullable=True,
                                                index=True)
    unit_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    artefact: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    pdf_generated: Mapped[bool] = mapped_column(default=False, nullable=False)
    document_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class EqaEnrolment(Base):
    """One enrolment in an external PT programme (BD-34)."""
    __tablename__ = "lab_eqa_enrolments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enrolment_id: Mapped[str] = mapped_column(String(120), unique=True,
                                              nullable=False, index=True)
    programme: Mapped[str] = mapped_column(String(200), nullable=False,
                                           index=True)
    provider: Mapped[str] = mapped_column(String(200), nullable=False,
                                          default="")
    analyte_codes: Mapped[list] = mapped_column(JSON, nullable=False,
                                                default=list)
    status: Mapped[str] = mapped_column(String(40), nullable=False,
                                        default="enrolled")
    created_by: Mapped[str] = mapped_column(String(120), nullable=False,
                                            default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


class EqaExchangeLog(Base):
    """One export/import exchange against an external PT programme
    (BD-34), fixture-defined format; live exchange is NOT RUN."""
    __tablename__ = "lab_eqa_exchange_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    programme: Mapped[str] = mapped_column(String(200), nullable=False,
                                           index=True)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    format_name: Mapped[str] = mapped_column(String(120), nullable=False,
                                             default="")
    rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Published config seeds
# ---------------------------------------------------------------------------

CLIA_ELEMENTS = [
    {"code": "direct-observation-performance",
     "display": "Direct observation of routine patient test performance"},
    {"code": "monitoring-recording-reporting",
     "display": "Monitoring the recording and reporting of test results"},
    {"code": "review-intermediate-qc-pt",
     "display": "Review of intermediate test results or worksheets, QC "
                "records, proficiency testing results, and preventive "
                "maintenance records"},
    {"code": "direct-observation-maintenance",
     "display": "Direct observation of performance of instrument "
                "maintenance and function checks"},
    {"code": "assessment-test-performance",
     "display": "Assessment of test performance through testing "
                "previously analysed specimens, internal blind testing "
                "samples or external proficiency testing samples"},
    {"code": "assessment-problem-solving",
     "display": "Assessment of problem-solving skills"},
]


def default_governance_payload() -> dict:
    return {
        "name": "OpenCare laboratory governance",
        "concepts": [
            {"code": "lab-director", "display": "Laboratory director",
             "active": True},
            {"code": "lab-technologist", "display": "Laboratory technologist",
             "active": True},
        ],
        # The job_role (staff directory) whose holders may compose
        # catalogue drafts alongside admin/configurator.
        "directorJobRole": "lab-director",
    }


def default_competency_payload() -> dict:
    return {
        "name": "OpenCare CLIA competency programme",
        "concepts": [
            {"code": e["code"], "display": e["display"], "active": True}
            for e in CLIA_ELEMENTS
        ],
        # The six CLIA competency elements (42 CFR 493.1413(b)(8) /
        # 493.1451(b)(8) model) — the operative list records are
        # validated and currency-computed against.
        "elements": copy.deepcopy(CLIA_ELEMENTS),
        "validityMonths": 12,
        "worklistWindowDays": 90,
        "expiringWindowDays": 30,
    }


def default_eqa_provider_payload() -> dict:
    return {
        "name": "Synthetic PT provider exchange (fixture-defined)",
        "provider": "Synthetic PT Provider (fixture)",
        "formatName": "opencare-eqaprovider-csv-v1",
        "basis": ("Fixture-defined provider format: the field map "
                  "below is this build's fixture contract with a "
                  "synthetic provider. Live provider exchange is "
                  "NOT RUN."),
        "fieldMap": {
            "programme": "Programme",
            "analyte_code": "AnalyteCode",
            "reported_value": "ReportedValue",
            "acceptable_low": "AcceptableLow",
            "acceptable_high": "AcceptableHigh",
        },
    }


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
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


def ensure_lab_depth_defaults(session: Session) -> None:
    _seed_published(session, "terminology", GOVERNANCE_KEY,
                    default_governance_payload())
    _seed_published(session, "terminology", COMPETENCY_KEY,
                    default_competency_payload())
    _seed_published(session, "integration", EQA_PROVIDER_KEY,
                    default_eqa_provider_payload())
    session.commit()


def _governance(session: Session) -> dict:
    cfg = published_config(session, "terminology", GOVERNANCE_KEY)
    return (cfg.payload or {}) if cfg is not None \
        else default_governance_payload()


def _competency_config(session: Session) -> dict:
    cfg = published_config(session, "terminology", COMPETENCY_KEY)
    return (cfg.payload or {}) if cfg is not None \
        else default_competency_payload()


def _eqa_provider(session: Session) -> dict:
    cfg = published_config(session, "integration", EQA_PROVIDER_KEY)
    return (cfg.payload or {}) if cfg is not None \
        else default_eqa_provider_payload()


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


# ---------------------------------------------------------------------------
# A. Test Manager + Panel Manager — enriched published views
# ---------------------------------------------------------------------------

DEFAULT_ALIQUOT_RULES = {"count": 1, "volumeMl": 5.0,
                         "container": "standard tube"}


def _enrich_test(session: Session, catalog: dict, concept: dict) -> dict:
    rules = concept.get("aliquotRules")
    has_rules = isinstance(rules, dict)
    reflex = [r for r in catalog.get("reflexRules") or []
              if isinstance(r, dict)
              and (r.get("triggerCode") == concept.get("code")
                   or r.get("addCode") == concept.get("code"))]
    return {
        "code": concept.get("code"), "display": concept.get("display"),
        "active": concept.get("active", True),
        "bench": concept.get("bench"),
        "unit": concept.get("unit"),
        "qualitative": bool(concept.get("qualitative")),
        "values": concept.get("values"),
        "reference_range": concept.get("referenceRange"),
        "aliquot_rules": rules if has_rules else dict(DEFAULT_ALIQUOT_RULES),
        "aliquot_rules_source": "catalog" if has_rules else "default",
        "specimen_requirements": {
            "specimen_type": concept.get("specimenType")
            or "Blood specimen",
            "container": (rules.get("container") if has_rules else None)
            or concept.get("container") or "standard tube",
            "required_aliquots": (rules.get("count") if has_rules else 1),
            "volume_ml": (rules.get("volumeMl") if has_rules else 5.0),
        },
        "reflex_rules": reflex,
        "panel": False,
    }


def _enrich_panel(session: Session, catalog: dict, concept: dict,
                  by_code: dict) -> dict:
    components = []
    for code in concept.get("components") or []:
        comp = by_code.get(code)
        components.append(
            _enrich_test(session, catalog, comp) if comp
            else {"code": code, "display": None, "missing": True})
    reflex = [r for r in catalog.get("reflexRules") or []
              if isinstance(r, dict)
              and (r.get("triggerCode") == concept.get("code")
                   or r.get("triggerCode") in (concept.get("components") or [])
                   or r.get("addCode") in (concept.get("components") or []))]
    rules = concept.get("aliquotRules")
    has_rules = isinstance(rules, dict)
    return {
        "code": concept.get("code"), "display": concept.get("display"),
        "active": concept.get("active", True),
        "bench": concept.get("bench"), "panel": True,
        "components": components,
        "component_codes": list(concept.get("components") or []),
        "reflex_rules": reflex,
        "aliquot_rules": rules if has_rules else dict(DEFAULT_ALIQUOT_RULES),
        "aliquot_rules_source": "catalog" if has_rules else "default",
        "specimen_requirements": {
            "specimen_type": concept.get("specimenType")
            or "Blood specimen",
            "container": (rules.get("container") if has_rules else None)
            or concept.get("container") or "standard tube",
            "required_aliquots": (rules.get("count") if has_rules else 1),
            "volume_ml": (rules.get("volumeMl") if has_rules else 5.0),
        },
    }


@router.get("/manager/catalog")
def manager_catalog(session: Session = Depends(get_db),
                    user: User = Depends(require_capability("lab.read"))):
    """Test Manager / Panel Manager home view: the PUBLISHED lab
    catalogue enriched with per-test aliquot/derivation rules,
    panel composition, reflex rules and specimen/container
    requirements — plus the current draft's summary when a draft
    exists (drafts themselves are Studio designer data)."""
    catalog = lab._catalog(session)
    cfg = published_config(session, CATALOG_KIND, lab.LAB_CATALOG_KEY)
    concepts = [c for c in catalog.get("concepts") or []
                if isinstance(c, dict)]
    by_code = {c["code"]: c for c in concepts if c.get("code")}
    tests = [_enrich_test(session, catalog, c) for c in concepts
             if not c.get("panel")]
    panels = [_enrich_panel(session, catalog, c, by_code) for c in concepts
              if c.get("panel")]
    versions = list_config_versions(session, CATALOG_KIND, lab.LAB_CATALOG_KEY)
    draft = next((v for v in versions if v.status == ConfigStatus.draft),
                 None)
    return {
        "catalog_version": cfg.version if cfg else None,
        "config": {"kind": CATALOG_KIND, "key": lab.LAB_CATALOG_KEY,
                   "publish_in": "System Studio -> Terminology -> lab-catalog"},
        "tests": tests, "panels": panels,
        "reflex_rules": catalog.get("reflexRules") or [],
        "draft": ({"version": draft.version,
                   "updated_by": draft.updated_by} if draft else None),
    }


@router.get("/manager/draft")
def manager_draft(session: Session = Depends(get_db),
                  user: User = Depends(require_capability("lab.read"))):
    versions = list_config_versions(session, CATALOG_KIND, lab.LAB_CATALOG_KEY)
    draft = next((v for v in versions if v.status == ConfigStatus.draft),
                 None)
    if draft is None:
        return {"draft": None}
    return {"draft": {"version": draft.version, "payload": draft.payload,
                      "updated_by": draft.updated_by}}


# ---------------------------------------------------------------------------
# A. Draft compose — Studio draft machinery against the catalogue
# ---------------------------------------------------------------------------

def _linked_staff(session: Session, user: User) -> list[StaffMember]:
    rows = list(session.scalars(select(StaffMember).where(
        StaffMember.username == user.username)).all())
    account = session.scalars(select(UserAccount).where(
        UserAccount.username == user.username)).first()
    if account is not None and account.staff_id:
        linked = session.scalars(select(StaffMember).where(
            StaffMember.staff_id == account.staff_id)).first()
        if linked is not None and linked not in rows:
            rows.append(linked)
    return rows


def _require_composer(session: Session, user: User) -> None:
    """Compose authorisation: admin/configurator by role, or the
    caller's linked staff job_role equals the laboratory-director
    role named in the published lab-governance config."""
    if user.role.value in ("admin", "configurator"):
        return
    director_role = _governance(session).get("directorJobRole") \
        or "lab-director"
    for staff in _linked_staff(session, user):
        if staff.job_role == director_role:
            return
    raise HTTPException(
        status_code=403,
        detail=(f"Catalogue composition requires the laboratory "
                f"director job role ('{director_role}') or the "
                "admin/configurator role; caller "
                f"'{user.username}' (role {user.role.value}) holds "
                "neither"))


def _validate_aliquot_rules(rules, field: str) -> dict:
    if not isinstance(rules, dict):
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be an object "
                                   "{count, volumeMl, container}")
    count = rules.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise HTTPException(status_code=422,
                            detail=f"'{field}.count' must be a positive "
                                   "integer (number of aliquots)")
    volume = rules.get("volumeMl")
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) \
            or float(volume) <= 0:
        raise HTTPException(status_code=422,
                            detail=f"'{field}.volumeMl' must be a "
                                   "positive number")
    container = rules.get("container")
    if not isinstance(container, str) or not container.strip():
        raise HTTPException(status_code=422,
                            detail=f"'{field}.container' must be a "
                                   "non-empty string")
    return {"count": count, "volumeMl": float(volume),
            "container": container.strip()}


def _draft_payload(session: Session) -> tuple[dict, StudioConfig | None]:
    """(working payload, draft row|None): a deep copy of the current
    draft when one exists, else of the published catalogue."""
    versions = list_config_versions(session, CATALOG_KIND, lab.LAB_CATALOG_KEY)
    draft = next((v for v in versions if v.status == ConfigStatus.draft),
                 None)
    if draft is not None:
        return copy.deepcopy(draft.payload or {}), draft
    published = published_config(session, CATALOG_KIND, lab.LAB_CATALOG_KEY)
    if published is None:
        raise HTTPException(
            status_code=503,
            detail="No published lab catalog to compose a draft from")
    return copy.deepcopy(published.payload or {}), None


def _save_draft(session: Session, user: User, payload: dict,
                draft_row: StudioConfig | None, action: str) -> StudioConfig:
    """Write the working payload onto the catalogue DRAFT only —
    the protocols.py create/update-draft pattern: update the open
    draft, or create the next version as a draft. The published row
    is never touched; terminology validity is enforced with the
    governed validator before anything is written."""
    try:
        validate_payload(CATALOG_KIND, payload)
    except ConfigPayloadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    if draft_row is not None:
        draft_row.payload = payload
        draft_row.updated_by = user.username
        session.flush()
        append_audit(session, actor=user.username, action=action,
                     entity=(f"config:{CATALOG_KIND}/"
                             f"{lab.LAB_CATALOG_KEY}@v{draft_row.version}"),
                     payload={"kind": CATALOG_KIND,
                              "key": lab.LAB_CATALOG_KEY,
                              "version": draft_row.version,
                              "via": "lab-manager"})
        session.commit()
        return draft_row
    versions = list_config_versions(session, CATALOG_KIND, lab.LAB_CATALOG_KEY)
    next_version = (versions[-1].version + 1) if versions else 1
    row = StudioConfig(kind=CATALOG_KIND, key=lab.LAB_CATALOG_KEY,
                       version=next_version, status=ConfigStatus.draft,
                       payload=payload, created_by=user.username,
                       updated_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action=action,
                 entity=f"config:{CATALOG_KIND}/{lab.LAB_CATALOG_KEY}"
                        f"@v{next_version}",
                 payload={"kind": CATALOG_KIND, "key": lab.LAB_CATALOG_KEY,
                          "version": next_version, "via": "lab-manager"})
    session.commit()
    return row


def _draft_out(row: StudioConfig) -> dict:
    return {"kind": row.kind, "key": row.key, "version": row.version,
            "status": row.status.value, "payload": row.payload,
            "publish_via": (f"POST /studio/config/{row.kind}/{row.key}"
                            f"/versions/{row.version}/publish")}


@router.post("/manager/tests/draft", status_code=201)
async def compose_test(request: Request, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.work"))):
    """Add or update a test entry (including its aliquotRules) on
    the catalogue DRAFT. The published catalogue is never mutated;
    the change takes effect only when the draft is published in
    Studio."""
    _require_composer(session, user)
    body = await _json_body(request)
    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(status_code=422, detail="'code' is required")
    display = body.get("display")
    if not isinstance(display, str) or not display.strip():
        raise HTTPException(status_code=422, detail="'display' is required")
    payload, draft_row = _draft_payload(session)
    concepts = [c for c in payload.get("concepts") or []
                if isinstance(c, dict)]
    existing = next((c for c in concepts if c.get("code") == code.strip()),
                    None)
    concept = dict(existing) if existing else {}
    concept.update({"code": code.strip(), "display": display.strip(),
                    "active": body.get("active", True) is not False})
    concept.pop("panel", None)
    concept.pop("components", None)
    for field, target in (("bench", "bench"), ("unit", "unit"),
                          ("specimen_type", "specimenType"),
                          ("container", "container")):
        if body.get(field) is not None:
            concept[target] = body[field]
    if body.get("qualitative") is True:
        values = body.get("values")
        if not isinstance(values, list) or not values:
            raise HTTPException(status_code=422,
                                detail="A qualitative test requires a "
                                       "non-empty 'values' list")
        concept["qualitative"] = True
        concept["values"] = list(values)
    elif body.get("reference_range") is not None:
        concept["referenceRange"] = body["reference_range"]
    if body.get("aliquot_rules") is not None:
        concept["aliquotRules"] = _validate_aliquot_rules(
            body["aliquot_rules"], "aliquot_rules")
    elif body.get("aliquotRules") is not None:
        concept["aliquotRules"] = _validate_aliquot_rules(
            body["aliquotRules"], "aliquotRules")
    if existing is None:
        concepts.append(concept)
    else:
        concepts[concepts.index(existing)] = concept
    payload["concepts"] = concepts
    row = _save_draft(session, user, payload, draft_row,
                      "lab.manager.test.draft")
    return {"draft": _draft_out(row), "test": concept}


@router.post("/manager/panels/draft", status_code=201)
async def compose_panel(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    """Add or update a panel definition on the catalogue DRAFT.
    Validation: every component must exist in the (draft) catalogue
    and must not itself be a panel; reflex rules, when supplied,
    must reference existing tests. Published payloads are never
    mutated — publish stays in Studio."""
    _require_composer(session, user)
    body = await _json_body(request)
    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(status_code=422, detail="'code' is required")
    display = body.get("display")
    if not isinstance(display, str) or not display.strip():
        raise HTTPException(status_code=422, detail="'display' is required")
    components = body.get("components")
    if not isinstance(components, list) or not components \
            or any(not isinstance(c, str) or not c for c in components):
        raise HTTPException(status_code=422,
                            detail="'components' must be a non-empty "
                                   "list of test codes")
    payload, draft_row = _draft_payload(session)
    concepts = [c for c in payload.get("concepts") or []
                if isinstance(c, dict)]
    by_code = {c["code"]: c for c in concepts if c.get("code")}
    for comp in components:
        target = by_code.get(comp)
        if target is None:
            raise HTTPException(
                status_code=422,
                detail=f"Panel component '{comp}' does not exist in "
                       "the lab catalogue")
        if target.get("panel"):
            raise HTTPException(
                status_code=422,
                detail=f"Panel component '{comp}' is itself a panel; "
                       "panels compose tests, not panels")
    reflex_in = body.get("reflex_rules") or body.get("reflexRules") or []
    if not isinstance(reflex_in, list):
        raise HTTPException(status_code=422,
                            detail="'reflex_rules' must be a list")
    known_codes = set(by_code) | {code.strip()}
    new_reflex = []
    for i, rule in enumerate(reflex_in):
        if not isinstance(rule, dict):
            raise HTTPException(status_code=422,
                                detail=f"reflex_rules[{i}] must be an object")
        trigger = rule.get("triggerCode") or rule.get("trigger_code")
        add = rule.get("addCode") or rule.get("add_code")
        if trigger not in known_codes:
            raise HTTPException(
                status_code=422,
                detail=f"reflex_rules[{i}] trigger '{trigger}' does not "
                       "reference an existing test")
        if add not in known_codes:
            raise HTTPException(
                status_code=422,
                detail=f"reflex_rules[{i}] add code '{add}' does not "
                       "reference an existing test")
        new_reflex.append({
            "id": rule.get("id") or f"reflex-{code.strip().lower()}-{i + 1}",
            "triggerCode": trigger, "when": rule.get("when") or "below-low",
            "addCode": add, "note": rule.get("note") or ""})
    existing = by_code.get(code.strip())
    concept = dict(existing) if existing else {}
    concept.update({"code": code.strip(), "display": display.strip(),
                    "active": body.get("active", True) is not False,
                    "panel": True, "components": list(components)})
    if body.get("bench") is not None:
        concept["bench"] = body["bench"]
    if body.get("specimen_type") is not None:
        concept["specimenType"] = body["specimen_type"]
    if body.get("aliquot_rules") is not None:
        concept["aliquotRules"] = _validate_aliquot_rules(
            body["aliquot_rules"], "aliquot_rules")
    elif body.get("aliquotRules") is not None:
        concept["aliquotRules"] = _validate_aliquot_rules(
            body["aliquotRules"], "aliquotRules")
    if existing is None:
        concepts.append(concept)
    else:
        concepts[concepts.index(existing)] = concept
    payload["concepts"] = concepts
    if new_reflex:
        rules = [r for r in payload.get("reflexRules") or []
                 if isinstance(r, dict)]
        ids = {r.get("id") for r in rules}
        for rule in new_reflex:
            if rule["id"] not in ids:
                rules.append(rule)
        payload["reflexRules"] = rules
    row = _save_draft(session, user, payload, draft_row,
                      "lab.manager.panel.draft")
    return {"draft": _draft_out(row), "panel": concept,
            "reflex_rules_added": new_reflex}


# ---------------------------------------------------------------------------
# C. BD-09 — blood-bank lookback + FDA deviation reporting
# ---------------------------------------------------------------------------

def _retention_block(created: datetime | None = None) -> dict:
    created = created or datetime.now(timezone.utc)
    until = (created.date() + timedelta(days=365 * RETENTION_YEARS))
    return {
        "regulation": RETENTION_CITATION,
        "retention_years": RETENTION_YEARS,
        "retain_until": until.isoformat(),
        "statement": ("Traceability records for this case are retained "
                      "for at least 10 years per 21 CFR 606.160 "
                      "(blood establishment record retention)."),
    }


def _transfusions_for(session: Session, unit_id: str) -> list[FhirResource]:
    out = []
    for proc in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Procedure")).all():
        for ext in (proc.body or {}).get("extension") or []:
            if isinstance(ext, dict) \
                    and ext.get("url") == "urn:opencare:extension/blood-unit" \
                    and ext.get("valueString") == unit_id:
                out.append(proc)
                break
    return out


def _lookback_out(session: Session, case: LookbackCase) -> dict:
    notifications = session.scalars(select(LookbackNotification).where(
        LookbackNotification.case_id == case.case_id)
        .order_by(LookbackNotification.id.asc())).all()
    return {
        "case_id": case.case_id, "trigger_unit_id": case.trigger_unit_id,
        "trigger_detail": case.trigger_detail, "status": case.status,
        "trace": case.trace or {},
        "traceability": _retention_block(case.created_at),
        "notifications": [
            {"id": n.id, "kind": n.kind, "recipient_ref": n.recipient_ref,
             "unit_id": n.unit_id, "state": n.state,
             "notified_by": n.notified_by,
             "notified_at": n.notified_at.isoformat() if n.notified_at
             else None}
            for n in notifications],
        "created_by": case.created_by,
        "created_at": case.created_at.isoformat() if case.created_at else None,
    }


@router.post("/blood-bank/lookback", status_code=201)
async def open_lookback(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.work"))):
    """Open a lookback case from a trigger (a donor/unit test
    result naming a unit): trace the unit to ALL implicated
    components (same order's units + units sharing the donation
    attributes product/group/expiry, issued or not) and their
    recipients, quarantine implicated UNISSUED units through the
    blood bank's own status machinery (the 'quarantined' status
    the reaction workflow uses), and create consignee/recipient
    notification records."""
    payload = await _json_body(request)
    unit_id = payload.get("unit_id") or payload.get("trigger_unit_id")
    if not unit_id:
        raise HTTPException(status_code=422,
                            detail="'unit_id' (the trigger unit) is required")
    trigger_detail = payload.get("trigger_detail") \
        or payload.get("test_result") or payload.get("reason") or ""
    trigger = lab_benches._unit_row(session, str(unit_id))
    # Implicated components: the trigger unit, every unit issued
    # under the same order (the components of the same issue event
    # on this spine), and every unit sharing the trigger's donation
    # attributes (product type + blood group + expiry — the
    # component set of one donation in this inventory model),
    # including units never issued.
    implicated = [trigger]
    seen_units = {trigger.unit_id}
    siblings = session.scalars(select(BloodUnit).where(
        BloodUnit.unit_id != trigger.unit_id)).all()
    for sibling in siblings:
        same_order = trigger.order_id and sibling.order_id == trigger.order_id
        same_donation = (
            sibling.product_type == trigger.product_type
            and sibling.blood_group == trigger.blood_group
            and sibling.expiry_date == trigger.expiry_date)
        if (same_order or same_donation) \
                and sibling.unit_id not in seen_units:
            seen_units.add(sibling.unit_id)
            implicated.append(sibling)
    components, recipients, quarantined = [], [], []
    seen_patients = set()
    for unit in implicated:
        procs = _transfusions_for(session, unit.unit_id)
        was = unit.status
        if unit.status == "available":
            unit.status = "quarantined"
            quarantined.append(unit.unit_id)
        components.append({
            "unit_id": unit.unit_id, "product_type": unit.product_type,
            "blood_group": unit.blood_group, "expiry_date": unit.expiry_date,
            "status_before": was, "status": unit.status,
            "order_id": unit.order_id, "patient_ref": unit.patient_ref,
            "transfusion_ids": [p.resource_id for p in procs],
        })
        if unit.patient_ref:
            key = (unit.patient_ref, unit.unit_id)
            if key not in seen_patients:
                seen_patients.add(key)
                recipients.append({"patient_ref": unit.patient_ref,
                                   "unit_id": unit.unit_id,
                                   "transfusion_ids":
                                   [p.resource_id for p in procs]})
    case_id = f"lookback-{uuid.uuid4().hex[:10]}"
    trace = {"trigger_unit_id": trigger.unit_id,
             "components": components, "recipients": recipients,
             "quarantined_units": quarantined,
             "component_count": len(components),
             "recipient_count": len(recipients)}
    case = LookbackCase(case_id=case_id, trigger_unit_id=trigger.unit_id,
                        trigger_detail=str(trigger_detail), status="open",
                        trace=trace, created_by=user.username)
    session.add(case)
    session.flush()
    for recipient in recipients:
        session.add(LookbackNotification(
            case_id=case_id, kind="recipient",
            recipient_ref=recipient["patient_ref"],
            unit_id=recipient["unit_id"], state="pending"))
    for q_unit in quarantined:
        session.add(LookbackNotification(
            case_id=case_id, kind="consignee",
            recipient_ref="blood-bank-inventory", unit_id=q_unit,
            state="pending"))
    session.flush()
    append_audit(session, actor=user.username, action="lab.lookback.open",
                 entity=f"LookbackCase/{case_id}",
                 payload={"trigger_unit_id": trigger.unit_id,
                          "components": len(components),
                          "recipients": len(recipients),
                          "quarantined": quarantined})
    session.commit()
    return _lookback_out(session, case)


@router.get("/blood-bank/lookback/{case_id}")
def get_lookback(case_id: str, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("lab.read"))):
    case = session.scalars(select(LookbackCase).where(
        LookbackCase.case_id == case_id)).first()
    if case is None:
        raise HTTPException(status_code=404,
                            detail=f"Lookback case '{case_id}' not found")
    return _lookback_out(session, case)


@router.get("/blood-bank/lookback")
def list_lookbacks(session: Session = Depends(get_db),
                   user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(select(LookbackCase).order_by(
        LookbackCase.id.desc())).all()
    return {"items": [_lookback_out(session, c) for c in rows],
            "total": len(rows)}


@router.post("/blood-bank/lookback/{case_id}/notifications/{notification_id}")
async def update_notification(case_id: str, notification_id: int,
                              request: Request,
                              session: Session = Depends(get_db),
                              user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    state = payload.get("state")
    if state not in ("notified", "acknowledged"):
        raise HTTPException(status_code=422,
                            detail="'state' must be 'notified' or "
                                   "'acknowledged'")
    note = session.scalars(select(LookbackNotification).where(
        LookbackNotification.case_id == case_id,
        LookbackNotification.id == notification_id)).first()
    if note is None:
        raise HTTPException(status_code=404,
                            detail="Notification not found for this case")
    note.state = state
    note.notified_by = user.username
    note.notified_at = datetime.now(timezone.utc)
    session.flush()
    append_audit(session, actor=user.username, action="lab.lookback.notify",
                 entity=f"LookbackCase/{case_id}",
                 payload={"notification_id": notification_id, "state": state})
    session.commit()
    case = session.scalars(select(LookbackCase).where(
        LookbackCase.case_id == case_id)).first()
    return _lookback_out(session, case)


def _deviation_out(row: DeviationReport) -> dict:
    return {"report_id": row.report_id, "case_id": row.case_id,
            "unit_id": row.unit_id, "description": row.description,
            "artefact": row.artefact or {},
            "pdf_generated": bool(row.pdf_generated),
            "document_note": row.document_note,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat()
            if row.created_at else None}


def _deviation_pdf(session: Session, row: DeviationReport) -> bytes | None:
    """Render the deviation report document with the Phase 13
    reports PDF machinery; None (with the reason on the row) when
    the machinery is unavailable."""
    try:
        from . import reports as reports_module
        template = reports_module._print_template(session)
        artefact = row.artefact or {}
        meta = [("Report", row.report_id),
                ("Lookback case", row.case_id or "—"),
                ("Unit", row.unit_id or "—"),
                ("Reported by", row.created_by),
                ("Reported at", row.created_at.isoformat()
                 if row.created_at else "")]
        columns = ["Unit", "Product", "Status", "Recipient"]
        rows = [[str(c.get("unit_id") or ""),
                 str(c.get("product_type") or ""),
                 str(c.get("status") or ""),
                 str(c.get("patient_ref") or "—")]
                for c in artefact.get("components") or []]
        notes = [row.description,
                 (artefact.get("traceability") or {}).get("statement", ""),
                 "Biological product deviation report — generated "
                 "from the lookback trace on file."]
        return reports_module.build_pdf(
            template, "Biological Product Deviation Report", meta,
            columns, rows, notes)
    except Exception:
        return None


@router.post("/blood-bank/deviations", status_code=201)
async def create_deviation(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    """Generate a biological-product deviation report from a
    lookback case (or a standalone deviation event naming a unit):
    a structured report record + a PDF document rendered with
    app.reports machinery when it imports cleanly; otherwise the
    structured artefact alone with the reason recorded."""
    payload = await _json_body(request)
    case_id = payload.get("case_id") or payload.get("lookback_case_id")
    unit_id = payload.get("unit_id")
    description = payload.get("description") or payload.get("detail") or ""
    if not isinstance(description, str) or not description.strip():
        raise HTTPException(status_code=422,
                            detail="'description' of the deviation is "
                                   "required")
    case = None
    if case_id:
        case = session.scalars(select(LookbackCase).where(
            LookbackCase.case_id == str(case_id))).first()
        if case is None:
            raise HTTPException(status_code=404,
                                detail=f"Lookback case '{case_id}' not found")
        unit_id = unit_id or case.trigger_unit_id
    artefact = {
        "type": "biological-product-deviation",
        "regulation": RETENTION_CITATION,
        "description": description.strip(),
        "components": (case.trace or {}).get("components") if case else [],
        "recipients": (case.trace or {}).get("recipients") if case else [],
        "traceability": _retention_block(),
        "basis": "synthetic-data",
    }
    row = DeviationReport(report_id=f"bpd-{uuid.uuid4().hex[:10]}",
                          case_id=str(case_id) if case_id else None,
                          unit_id=str(unit_id) if unit_id else None,
                          description=description.strip(),
                          artefact=artefact, pdf_generated=False,
                          created_by=user.username)
    session.add(row)
    session.flush()
    pdf = _deviation_pdf(session, row)
    if pdf is not None:
        row.pdf_generated = True
        row.document_note = f"PDF document generated ({len(pdf)} bytes) via app.reports.build_pdf"
    else:
        row.document_note = ("PDF machinery unavailable in this build; "
                             "the structured artefact above is the "
                             "report of record")
    session.flush()
    append_audit(session, actor=user.username, action="lab.deviation.create",
                 entity=f"DeviationReport/{row.report_id}",
                 payload={"case_id": row.case_id, "unit_id": row.unit_id,
                          "pdf_generated": row.pdf_generated})
    session.commit()
    return _deviation_out(row)


@router.get("/blood-bank/deviations/{report_id}")
def get_deviation(report_id: str, session: Session = Depends(get_db),
                  user: User = Depends(require_capability("lab.read"))):
    row = session.scalars(select(DeviationReport).where(
        DeviationReport.report_id == report_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Deviation report '{report_id}' not found")
    return _deviation_out(row)


@router.get("/blood-bank/deviations/{report_id}/document")
def deviation_document(report_id: str, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("lab.read"))):
    row = session.scalars(select(DeviationReport).where(
        DeviationReport.report_id == report_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Deviation report '{report_id}' not found")
    pdf = _deviation_pdf(session, row)
    if pdf is None:
        raise HTTPException(status_code=503,
                            detail="PDF machinery unavailable in this build")
    append_audit(session, actor=user.username,
                 action="reports.document.print",
                 entity=f"document:deviation/{report_id}",
                 payload={"document": "deviation", "reference": report_id,
                          "format": "pdf"})
    session.commit()
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="{report_id}.pdf"'})


@router.get("/blood-bank/deviations")
def list_deviations(session: Session = Depends(get_db),
                    user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(select(DeviationReport).order_by(
        DeviationReport.id.desc())).all()
    return {"items": [_deviation_out(r) for r in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# D. BD-10 — CLIA six-element competency
# ---------------------------------------------------------------------------

def _competency_out(session: Session, config: dict,
                    row: CliaCompetencyRecord) -> dict:
    elements_cfg = [e for e in config.get("elements") or []
                    if isinstance(e, dict) and e.get("code")]
    validity = config.get("validityMonths")
    if not isinstance(validity, int) or isinstance(validity, bool) \
            or validity <= 0:
        validity = 12
    window = config.get("expiringWindowDays")
    if not isinstance(window, int) or isinstance(window, bool) or window < 0:
        window = 30
    assessed = {e.get("element"): e for e in row.elements or []
                if isinstance(e, dict)}
    element_rows, expiries, missing = [], [], []
    today = date.today()
    for element in elements_cfg:
        code = element["code"]
        entry = assessed.get(code)
        if entry is None or not entry.get("assessed_on"):
            missing.append(code)
            element_rows.append({"element": code,
                                 "display": element.get("display"),
                                 "assessed_on": None, "assessor": None,
                                 "expires_on": None, "status": "missing"})
            continue
        try:
            assessed_on = date.fromisoformat(str(entry["assessed_on"]))
        except ValueError:
            missing.append(code)
            element_rows.append({"element": code,
                                 "display": element.get("display"),
                                 "assessed_on": entry.get("assessed_on"),
                                 "assessor": entry.get("assessor"),
                                 "expires_on": None, "status": "unknown"})
            continue
        expires = lab._add_months(assessed_on, validity)
        expiries.append(expires)
        element_rows.append({
            "element": code, "display": element.get("display"),
            "assessed_on": assessed_on.isoformat(),
            "assessor": entry.get("assessor"),
            "expires_on": expires.isoformat(),
            "status": "expired" if today > expires else "current"})
    staff = session.scalars(select(StaffMember).where(
        StaffMember.staff_id == row.staff_id)).first()
    if missing:
        status, expires_on, days = "incomplete", None, None
    elif expiries:
        expires_on_date = min(expiries)
        expires_on = expires_on_date.isoformat()
        days = (expires_on_date - today).days
        status = "expired" if days < 0 else \
            ("expiring" if days <= window else "current")
    else:
        status, expires_on, days = "incomplete", None, None
    return {"id": row.id, "staff_id": row.staff_id,
            "staff_name": staff.full_name if staff else None,
            "test_system": row.test_system, "elements": element_rows,
            "missing_elements": missing, "status": status,
            "expires_on": expires_on, "days_to_expiry": days,
            "validity_months": validity,
            "recorded_by": row.recorded_by,
            "created_at": row.created_at.isoformat()
            if row.created_at else None}


@router.get("/competency/config")
def competency_config(session: Session = Depends(get_db),
                      user: User = Depends(require_capability("lab.read"))):
    config = _competency_config(session)
    return {"elements": config.get("elements") or [],
            "validity_months": config.get("validityMonths"),
            "worklist_window_days": config.get("worklistWindowDays"),
            "expiring_window_days": config.get("expiringWindowDays"),
            "config": {"kind": "terminology", "key": COMPETENCY_KEY}}


@router.post("/competency/records", status_code=201)
async def create_competency(request: Request,
                            session: Session = Depends(get_db),
                            user: User = Depends(require_capability("lab.work"))):
    """Record a CLIA competency assessment for one testing person
    (staff directory) on one test system (catalogue test): every
    supplied element must be one of the six published CLIA
    elements, with an ISO assessment date and an assessor."""
    payload = await _json_body(request)
    config = _competency_config(session)
    staff_id = payload.get("staff_id")
    staff = session.scalars(select(StaffMember).where(
        StaffMember.staff_id == str(staff_id))).first() if staff_id else None
    if staff is None:
        raise HTTPException(status_code=422,
                            detail=f"Staff member '{staff_id}' is not in "
                                   "the staff directory")
    test_system = payload.get("test_system") or payload.get("test_code")
    if not test_system or lab._concept(session, str(test_system)) is None:
        raise HTTPException(
            status_code=422,
            detail=f"'test_system' '{test_system}' is not in the "
                   "published lab catalog")
    elements_in = payload.get("elements")
    if not isinstance(elements_in, list) or not elements_in:
        raise HTTPException(status_code=422,
                            detail="'elements' must be a non-empty list of "
                                   "{element, assessed_on, assessor}")
    known = {e["code"] for e in config.get("elements") or []
             if isinstance(e, dict) and e.get("code")}
    elements = []
    for i, entry in enumerate(elements_in):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422,
                                detail=f"elements[{i}] must be an object")
        code = entry.get("element") or entry.get("code")
        if code not in known:
            raise HTTPException(
                status_code=422,
                detail=f"elements[{i}] '{code}' is not one of the six "
                       f"published CLIA elements {sorted(known)}")
        try:
            assessed = date.fromisoformat(str(entry.get("assessed_on")))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail=f"elements[{i}].assessed_on must be an ISO date")
        if assessed > date.today():
            raise HTTPException(
                status_code=422,
                detail=f"elements[{i}].assessed_on cannot be in the future")
        assessor = entry.get("assessor")
        if not isinstance(assessor, str) or not assessor.strip():
            raise HTTPException(
                status_code=422,
                detail=f"elements[{i}].assessor is required")
        elements.append({"element": code,
                         "assessed_on": assessed.isoformat(),
                         "assessor": assessor.strip()})
    existing = session.scalars(select(CliaCompetencyRecord).where(
        CliaCompetencyRecord.staff_id == staff.staff_id,
        CliaCompetencyRecord.test_system == str(test_system))).first()
    if existing is not None:
        merged = {e["element"]: e for e in existing.elements or []}
        for entry in elements:
            merged[entry["element"]] = entry
        existing.elements = list(merged.values())
        existing.recorded_by = user.username
        row = existing
    else:
        row = CliaCompetencyRecord(staff_id=staff.staff_id,
                                   test_system=str(test_system),
                                   elements=elements,
                                   recorded_by=user.username)
        session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="lab.competency.record",
                 entity=f"Staff/{staff.staff_id}",
                 payload={"test_system": str(test_system),
                          "elements": [e["element"] for e in elements]})
    session.commit()
    return {"competency": _competency_out(session, config, row)}


@router.get("/competency/records")
def list_competency(staff_id: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("lab.read"))):
    config = _competency_config(session)
    stmt = select(CliaCompetencyRecord).order_by(CliaCompetencyRecord.id.asc())
    if staff_id:
        stmt = stmt.where(CliaCompetencyRecord.staff_id == staff_id)
    rows = session.scalars(stmt).all()
    return {"items": [_competency_out(session, config, r) for r in rows],
            "total": len(rows)}


@router.get("/competency/worklist")
def competency_worklist(session: Session = Depends(get_db),
                        user: User = Depends(require_capability("lab.read"))):
    """The competency worklist, shaped like the Phase 13 workforce
    credential worklist: expired + incomplete records plus everything
    expiring inside the published worklist window, soonest first."""
    config = _competency_config(session)
    worklist_window = config.get("worklistWindowDays")
    if not isinstance(worklist_window, int) \
            or isinstance(worklist_window, bool) or worklist_window <= 0:
        worklist_window = 90
    rows = session.scalars(select(CliaCompetencyRecord)).all()
    items = []
    for row in rows:
        entry = _competency_out(session, config, row)
        if entry["status"] in ("expired", "incomplete"):
            items.append(entry)
        elif entry["days_to_expiry"] is not None \
                and entry["days_to_expiry"] <= worklist_window:
            items.append(entry)
    items.sort(key=lambda i: (i["days_to_expiry"] is None,
                              i["days_to_expiry"] or 0))
    return {"items": items, "total": len(items),
            "worklist_window_days": worklist_window,
            "expired": sum(1 for i in items if i["status"] == "expired"),
            "incomplete": sum(1 for i in items
                              if i["status"] == "incomplete")}


# ---------------------------------------------------------------------------
# E. BD-34 — external EQA/PT exchange (fixture-defined; live NOT RUN)
# ---------------------------------------------------------------------------

def _enrolment_out(row: EqaEnrolment) -> dict:
    return {"enrolment_id": row.enrolment_id, "programme": row.programme,
            "provider": row.provider, "analyte_codes": row.analyte_codes or [],
            "status": row.status, "created_by": row.created_by,
            "created_at": row.created_at.isoformat()
            if row.created_at else None}


@router.post("/eqa-exchange/enrolments", status_code=201)
async def create_enrolment(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("lab.work"))):
    payload = await _json_body(request)
    programme = payload.get("programme")
    if not isinstance(programme, str) or not programme.strip():
        raise HTTPException(status_code=422, detail="'programme' is required")
    codes = payload.get("analyte_codes") or []
    if not isinstance(codes, list):
        raise HTTPException(status_code=422,
                            detail="'analyte_codes' must be a list")
    for code in codes:
        if lab._concept(session, str(code)) is None:
            raise HTTPException(
                status_code=422,
                detail=f"Analyte '{code}' is not in the published lab catalog")
    provider_cfg = _eqa_provider(session)
    row = EqaEnrolment(
        enrolment_id=f"eqa-enrol-{uuid.uuid4().hex[:8]}",
        programme=programme.strip(),
        provider=str(payload.get("provider")
                     or provider_cfg.get("provider") or ""),
        analyte_codes=[str(c) for c in codes], status="enrolled",
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username, action="lab.eqa.enrol",
                 entity=f"EqaEnrolment/{row.enrolment_id}",
                 payload={"programme": row.programme,
                          "analyte_codes": row.analyte_codes})
    session.commit()
    return _enrolment_out(row)


@router.get("/eqa-exchange/enrolments")
def list_enrolments(session: Session = Depends(get_db),
                    user: User = Depends(require_capability("lab.read"))):
    rows = session.scalars(select(EqaEnrolment).order_by(
        EqaEnrolment.id.desc())).all()
    return {"items": [_enrolment_out(r) for r in rows], "total": len(rows)}


@router.get("/eqa-exchange/export")
def eqa_export(programme: str = Query(...),
               session: Session = Depends(get_db),
               user: User = Depends(require_capability("lab.read"))):
    """Export enrolment + filed results for one programme in the
    provider format defined by published config
    (integration/eqa-provider: format name + field map). The format
    is FIXTURE-DEFINED; live provider exchange is NOT RUN."""
    provider = _eqa_provider(session)
    field_map = provider.get("fieldMap") or {}
    enrolment = session.scalars(select(EqaEnrolment).where(
        EqaEnrolment.programme == programme)).first()
    if enrolment is None:
        raise HTTPException(status_code=404,
                            detail=f"No EQA enrolment for programme "
                                   f"'{programme}'")
    records = session.scalars(select(LabEqaRecord).where(
        LabEqaRecord.event == programme).order_by(
        LabEqaRecord.id.asc())).all()
    headers = [field_map.get(f, f) for f in
               ("programme", "analyte_code", "reported_value",
                "acceptable_low", "acceptable_high")]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for record in records:
        writer.writerow([programme, record.analyte_code,
                         record.reported_value, record.acceptable_low,
                         record.acceptable_high])
    content = buffer.getvalue()
    session.add(EqaExchangeLog(
        programme=programme, direction="export",
        format_name=provider.get("formatName") or "", rows=len(records),
        detail={"enrolment_id": enrolment.enrolment_id,
                "basis": provider.get("basis")}, actor=user.username))
    session.flush()
    append_audit(session, actor=user.username, action="lab.eqa.export",
                 entity=f"EqaEnrolment/{enrolment.enrolment_id}",
                 payload={"programme": programme, "rows": len(records),
                          "format": provider.get("formatName"),
                          "live_exchange": "NOT RUN"})
    session.commit()
    return {"programme": programme,
            "format": provider.get("formatName"),
            "field_map": field_map, "content": content,
            "rows": len(records),
            "enrolment": _enrolment_out(enrolment),
            "basis": provider.get("basis"),
            "live_exchange": "NOT RUN — fixture-defined format only"}


@router.post("/eqa-exchange/import", status_code=201)
async def eqa_import(request: Request, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("lab.work"))):
    """Import returned scores for one programme: CSV content in the
    published provider format (field map applied in reverse) or a
    structured `scores` list. Scores are filed onto the internal
    EQA machinery's records (LabEqaRecord — the app.lab_benches
    programme store) with pass/fail computed server-side from the
    acceptable range, exactly as the internal EQA endpoint does."""
    payload = await _json_body(request)
    programme = payload.get("programme")
    if not isinstance(programme, str) or not programme.strip():
        raise HTTPException(status_code=422, detail="'programme' is required")
    enrolment = session.scalars(select(EqaEnrolment).where(
        EqaEnrolment.programme == programme.strip())).first()
    if enrolment is None:
        raise HTTPException(status_code=404,
                            detail=f"No EQA enrolment for programme "
                                   f"'{programme.strip()}'")
    provider = _eqa_provider(session)
    field_map = provider.get("fieldMap") or {}
    reverse = {v: k for k, v in field_map.items()}
    scores: list[dict] = []
    if isinstance(payload.get("scores"), list):
        scores = payload["scores"]
    elif isinstance(payload.get("content"), str):
        reader = csv.DictReader(io.StringIO(payload["content"]))
        for raw in reader:
            row = {}
            for header, value in raw.items():
                if header is None:
                    continue
                row[reverse.get(header.strip(), header.strip())] = value
            scores.append(row)
    else:
        raise HTTPException(status_code=422,
                            detail="Import requires 'content' (CSV in the "
                                   "provider format) or a 'scores' list")
    if not scores:
        raise HTTPException(status_code=422,
                            detail="Import carries no score rows")
    created = []
    for i, score in enumerate(scores):
        if not isinstance(score, dict):
            raise HTTPException(status_code=422,
                                detail=f"scores[{i}] must be an object")
        code = score.get("analyte_code")
        if not code or lab._concept(session, str(code)) is None:
            raise HTTPException(
                status_code=422,
                detail=f"scores[{i}] analyte '{code}' is not in the "
                       "published lab catalog")
        numbers = {}
        for field in ("reported_value", "acceptable_low", "acceptable_high"):
            try:
                numbers[field] = float(score.get(field))
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail=f"scores[{i}] '{field}' must be a number")
        record = LabEqaRecord(
            event=programme.strip(), analyte_code=str(code),
            reported_value=numbers["reported_value"],
            acceptable_low=numbers["acceptable_low"],
            acceptable_high=numbers["acceptable_high"],
            passed=numbers["acceptable_low"] <= numbers["reported_value"]
            <= numbers["acceptable_high"],
            recorded_by=user.username)
        session.add(record)
        session.flush()
        created.append(record)
    passed = sum(1 for r in created if r.passed)
    log = EqaExchangeLog(
        programme=programme.strip(), direction="import",
        format_name=provider.get("formatName") or "", rows=len(created),
        detail={"passed": passed, "failed": len(created) - passed,
                "scoring_state": "scored",
                "basis": provider.get("basis")}, actor=user.username)
    session.add(log)
    session.flush()
    append_audit(session, actor=user.username, action="lab.eqa.import",
                 entity=f"EqaEnrolment/{enrolment.enrolment_id}",
                 payload={"programme": programme.strip(),
                          "rows": len(created), "passed": passed,
                          "live_exchange": "NOT RUN"})
    session.commit()
    return {"programme": programme.strip(), "imported": len(created),
            "passed": passed, "failed": len(created) - passed,
            "scoring_state": "scored",
            "records": [{"id": r.id, "event": r.event,
                         "analyte_code": r.analyte_code,
                         "reported_value": r.reported_value,
                         "acceptable_low": r.acceptable_low,
                         "acceptable_high": r.acceptable_high,
                         "passed": r.passed}
                        for r in created],
            "basis": provider.get("basis"),
            "live_exchange": "NOT RUN — fixture-defined format only"}
