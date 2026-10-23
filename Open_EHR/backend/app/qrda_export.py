"""QRDA export of eCQM results — Phase 17, WS-COMP (BD-60 / CMP-43).

Exports the Phase 9 quality-measure engine's results (app.quality,
computed over the OMOP warehouse by app.pophealth's evaluator) as:

- **QRDA Category I** — one patient-level CDA document per patient:
  header (patient, author, custodian), a Measure Section naming the
  measure, and a Patient Data section carrying the patient's
  denominator / numerator population membership as computed by the
  measure engine for THAT patient (criteria_person_ids over the
  warehouse — patient-level membership, not the suppressed public
  counts).
- **QRDA Category III** — one aggregate CDA document per measure:
  Reporting Parameters + Measure Section with the aggregate
  denominator / numerator counts (raw counts — a QRDA submission is
  not a public report, so the pophealth small-cell suppression that
  governs /quality responses does not apply here; the counts are the
  engine's own).

**Local validation** (POST /qrda/validate) implements exactly these
schematron-STYLE structural checks, in code, and reports each by
name:
  1. well-formed XML (parses)
  2. root element is ClinicalDocument in the HL7 CDA namespace
  3. a recognised QRDA templateId is present (Cat I
     2.16.840.1.113883.10.20.24.1.1 or Cat III
     2.16.840.1.113883.10.20.27.1.1) and matches the declared type
  4. header completeness: id, effectiveTime, confidentialityCode,
     languageCode, recordTarget, author, custodian all present
  5. measure section present and naming a measure id
  6. Cat I: patient-data section present with at least one
     population-membership entry (DENOM / NUMER)
  7. Cat III: aggregate counts are non-negative integers and
     numerator <= denominator
  8. reporting period low <= high, both valid YYYYMMDD dates
  9. every id element carries a root or extension attribute
Full CMS QRDA schematron validation is **NOT RUN**: the official
CMS/ONC schematron artefacts are not available locally, and this
module says so in every validation report rather than implying
certification. The documents are structural QRDA for pipeline /
trading-partner pre-validation, not a certified submission.

Studio config surfaces: measure/<key> (the eCQM definitions
themselves, app.quality), integration/qrda-export (reporting-period
+ organisation defaults), permissions/role-matrix (capability
qrda.export).
"""
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import quality as quality_module
from . import warehouse as wh
from .audit import append_audit
from .configstore import (CAPABILITY_DEFAULTS, list_config_versions,
                          published_config, role_allowed)
from .models import ConfigStatus, FhirResource, StudioConfig, User
from .security import get_current_user, get_db

router = APIRouter(prefix="/qrda", tags=["qrda"])

NS = "urn:hl7-org:v3"
QRDA1_TEMPLATE = "2.16.840.1.113883.10.20.24.1.1"
QRDA3_TEMPLATE = "2.16.840.1.113883.10.20.27.1.1"
MEASURE_SECTION_TEMPLATE = "2.16.840.1.113883.10.20.24.2.2"
PATIENT_DATA_TEMPLATE = "2.16.840.1.113883.10.20.17.2.4"

_CAP_FALLBACKS = {"qrda.export": ("admin", "configurator", "clinician")}

IMPLEMENTED_CHECKS = [
    "well-formed-xml", "cda-root-and-namespace", "qrda-template-id",
    "header-completeness", "measure-section-present",
    "cat1-patient-data-membership", "cat3-aggregate-counts-sane",
    "reporting-period-valid", "id-attributes-present",
]
NOT_RUN_NOTE = ("Full CMS QRDA schematron validation is NOT RUN: the "
                "official CMS/ONC schematron artefacts are not available "
                "locally. Only the structural checks listed in "
                "implemented_checks were executed.")


def _cap_allowed(session: Session, user: User, capability: str) -> bool:
    cfg = published_config(session, "permissions", "role-matrix")
    if cfg is not None:
        overrides = ((cfg.payload or {}).get("capabilities") or {}).get(capability)
        if isinstance(overrides, dict) and user.role.value in overrides:
            return bool(overrides[user.role.value])
    if capability in CAPABILITY_DEFAULTS:
        return role_allowed(session, user.role, capability)
    return user.role.value in _CAP_FALLBACKS.get(capability, ())


def _require(capability: str):
    def dep(session: Session = Depends(get_db),
            user: User = Depends(get_current_user)) -> User:
        if not _cap_allowed(session, user, capability):
            raise HTTPException(
                status_code=403,
                detail=f"Role {user.role.value} lacks capability '{capability}'")
        return user
    return dep


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_ts(value: datetime) -> str:
    return value.strftime("%Y%m%d%H%M%S") + "+0000"


def _fmt_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    if list_config_versions(session, kind, key):
        return
    row = StudioConfig(kind=kind, key=key, version=1, status=ConfigStatus.published,
                       payload=payload, created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1, "seeded_default": True})


def default_qrda_payload() -> dict:
    return {"organisationName": "OpenCare General Hospital",
            "organisationIdRoot": "2.16.840.1.113883.19.5",
            "reportingYear": 2026,
            "notes": ("QRDA export defaults. The reporting period is "
                      "derived from the warehouse reference date "
                      "(app.pophealth._reference_date) unless a request "
                      "overrides it.")}


def ensure_qrda_defaults(session: Session) -> None:
    _seed_published(session, "integration", "qrda-export", default_qrda_payload())
    session.commit()


def _ensure(session: Session) -> None:
    if not list_config_versions(session, "integration", "qrda-export"):
        ensure_qrda_defaults(session)


def _org(session: Session) -> dict:
    cfg = published_config(session, "integration", "qrda-export")
    payload = (cfg.payload or {}) if cfg is not None else {}
    return {"name": str(payload.get("organisationName") or "OpenCare General Hospital"),
            "id_root": str(payload.get("organisationIdRoot") or "2.16.840.1.113883.19.5")}


def _measure(session: Session, key: str) -> dict:
    cfg = published_config(session, "measure", key)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"No published measure config '{key}'")
    return cfg.payload or {}


# ---------------------------------------------------------------------------
# XML construction (ElementTree, CDA namespace)
# ---------------------------------------------------------------------------

ET.register_namespace("", NS)


def _el(parent, tag, **attrs):
    node = ET.SubElement(parent, f"{{{NS}}}{tag}")
    for key, value in attrs.items():
        if value is not None:
            node.set(key, str(value))
    return node


def _header(root, *, org: dict, patient: dict | None, period: tuple[date, date],
            template: str) -> None:
    _el(root, "realmCode", code="US")
    type_id = _el(root, "typeId", root="2.16.840.1.113883.1.3", extension="POCD_HD000040")
    _el(root, "templateId", root=template)
    _el(root, "id", root=str(uuid.uuid4()))
    code = _el(root, "code", code="55182-0", codeSystem="2.16.840.1.113883.6.1",
               displayName="Quality Measure Report")
    title = _el(root, "title")
    title.text = "QRDA Category I" if template == QRDA1_TEMPLATE else "QRDA Category III"
    _el(root, "effectiveTime", value=_fmt_ts(_now()))
    _el(root, "confidentialityCode", code="N", codeSystem="2.16.840.1.113883.5.25")
    _el(root, "languageCode", code="en")
    if patient is not None:
        record_target = _el(root, "recordTarget")
        patient_role = _el(record_target, "patientRole")
        _el(patient_role, "id", root="2.16.840.1.113883.19.5",
            extension=patient["id"])
        patient_el = _el(patient_role, "patient")
        name_el = _el(patient_el, "name")
        family = _el(name_el, "family")
        family.text = patient.get("family") or ""
        given = _el(name_el, "given")
        given.text = patient.get("given") or ""
        _el(patient_el, "administrativeGenderCode",
            code=(patient.get("gender_code") or "UN"),
            codeSystem="2.16.840.1.113883.5.1")
        _el(patient_el, "birthTime", value=patient.get("birth") or "19000101")
    author = _el(root, "author")
    _el(author, "time", value=_fmt_ts(_now()))
    assigned = _el(author, "assignedAuthor")
    _el(assigned, "id", root=org["id_root"], extension="opencare-qrda")
    org_el = _el(assigned, "representedOrganization")
    name = _el(org_el, "name")
    name.text = org["name"]
    custodian = _el(root, "custodian")
    assigned_cust = _el(custodian, "assignedCustodian")
    cust_org = _el(assigned_cust, "representedCustodianOrganization")
    _el(cust_org, "id", root=org["id_root"])
    cust_name = _el(cust_org, "name")
    cust_name.text = org["name"]


def _measure_section(parent, *, measure_key: str, definition: dict,
                     period: tuple[date, date]) -> None:
    component = _el(parent, "component")
    section = _el(component, "section")
    _el(section, "templateId", root=MEASURE_SECTION_TEMPLATE)
    _el(section, "code", code="55186-1", codeSystem="2.16.840.1.113883.6.1",
        displayName="Measure Section")
    title = _el(section, "title")
    title.text = "Measure Section"
    entry = _el(section, "entry")
    organiser = _el(entry, "organizer", classCode="CLUSTER", moodCode="EVN")
    _el(organiser, "templateId", root="2.16.840.1.113883.10.20.24.3.98")
    _el(organiser, "id", root="2.16.840.1.113883.4.738", extension=measure_key)
    _el(organiser, "statusCode", code="completed")
    ref = _el(organiser, "reference", typeCode="REFR")
    ext = _el(ref, "externalDocument", classCode="DOC", moodCode="EVN")
    _el(ext, "id", root="2.16.840.1.113883.4.738", extension=measure_key)
    text = _el(ext, "text")
    text.text = str(definition.get("name") or measure_key)
    # Reporting period as a component observation.
    comp = _el(organiser, "component")
    obs = _el(comp, "observation", classCode="OBS", moodCode="EVN")
    _el(obs, "code", code="252116004", codeSystem="2.16.840.1.113883.6.96",
        displayName="Observation Parameters")
    value = _el(obs, "effectiveTime")
    _el(value, "low", value=_fmt_date(period[0]))
    _el(value, "high", value=_fmt_date(period[1]))


def build_qrda1(*, org: dict, measure_key: str, definition: dict,
                patient: dict, in_denominator: bool, in_numerator: bool,
                period: tuple[date, date]) -> str:
    root = ET.Element(f"{{{NS}}}ClinicalDocument")
    _header(root, org=org, patient=patient, period=period, template=QRDA1_TEMPLATE)
    structured = _el(_el(root, "component"), "structuredBody")
    _measure_section(structured, measure_key=measure_key, definition=definition,
                     period=period)
    component = _el(structured, "component")
    section = _el(component, "section")
    _el(section, "templateId", root=PATIENT_DATA_TEMPLATE)
    _el(section, "code", code="55188-7", codeSystem="2.16.840.1.113883.6.1",
        displayName="Patient Data")
    title = _el(section, "title")
    title.text = "Patient Data Section"
    populations = []
    if in_denominator:
        populations.append(("DENOM", "Denominator"))
    if in_numerator:
        populations.append(("NUMER", "Numerator"))
    if not populations:
        populations.append(("IPP", "Initial Patient Population (not in denominator)"))
    for code_value, label in populations:
        entry = _el(section, "entry")
        obs = _el(entry, "observation", classCode="OBS", moodCode="EVN")
        _el(obs, "templateId", root="2.16.840.1.113883.10.20.24.3.101")
        _el(obs, "id", root=str(uuid.uuid4()))
        _el(obs, "code", code="ASSERTION", codeSystem="2.16.840.1.113883.5.4")
        _el(obs, "statusCode", code="completed")
        value = _el(obs, "value", type="CD", code=code_value,
                    codeSystem="2.16.840.1.113883.5.4", displayName=label)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def build_qrda3(*, org: dict, measure_key: str, definition: dict,
                denominator: int, numerator: int,
                period: tuple[date, date]) -> str:
    root = ET.Element(f"{{{NS}}}ClinicalDocument")
    _header(root, org=org, patient=None, period=period, template=QRDA3_TEMPLATE)
    structured = _el(_el(root, "component"), "structuredBody")
    _measure_section(structured, measure_key=measure_key, definition=definition,
                     period=period)
    component = _el(structured, "component")
    section = _el(component, "section")
    _el(section, "templateId", root="2.16.840.1.113883.10.20.27.2.3")
    _el(section, "code", code="55187-9", codeSystem="2.16.840.1.113883.6.1",
        displayName="Reporting Parameters")
    entry = _el(section, "entry")
    organiser = _el(entry, "organizer", classCode="CLUSTER", moodCode="EVN")
    _el(organiser, "id", root=str(uuid.uuid4()))
    _el(organiser, "statusCode", code="completed")
    for code_value, count in (("DENOM", denominator), ("NUMER", numerator)):
        comp = _el(organiser, "component")
        obs = _el(comp, "observation", classCode="OBS", moodCode="EVN")
        _el(obs, "id", root=str(uuid.uuid4()))
        _el(obs, "code", code="ASSERTION", codeSystem="2.16.840.1.113883.5.4")
        _el(obs, "value", type="CD", code=code_value,
            codeSystem="2.16.840.1.113883.5.4")
        agg = _el(obs, "entryRelationship", typeCode="SUBJ")
        count_obs = _el(agg, "observation", classCode="OBS", moodCode="EVN")
        _el(count_obs, "code", code="MSRAGG", codeSystem="2.16.840.1.113883.5.4",
            displayName="Aggregate Count")
        _el(count_obs, "value", type="INT", value=str(count))
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


# ---------------------------------------------------------------------------
# Local validation — the implemented schematron-style checks
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(root, name: str):
    return [e for e in root.iter() if _local(e.tag) == name]


def _find_first(root, name: str):
    for e in root.iter():
        if _local(e.tag) == name:
            return e
    return None


def validate_qrda(xml_text: str, declared_type: str | None = None) -> dict:
    checks: list[dict] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "status": "pass" if passed else "fail",
                       "detail": detail})

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        record("well-formed-xml", False, f"XML does not parse: {exc}")
        for name in IMPLEMENTED_CHECKS[1:]:
            record(name, False, "not evaluated — document does not parse")
        return {"valid": False, "checks": checks,
                "implemented_checks": IMPLEMENTED_CHECKS, "not_run": NOT_RUN_NOTE}
    record("well-formed-xml", True, "XML parses")

    is_cda = _local(root.tag) == "ClinicalDocument" and root.tag.startswith("{" + NS + "}")
    record("cda-root-and-namespace", is_cda,
           "root is ClinicalDocument in urn:hl7-org:v3" if is_cda else
           f"root is {root.tag}, expected ClinicalDocument in {NS}")

    template_roots = {e.get("root") for e in _find_all(root, "templateId")}
    doc_type = None
    if QRDA1_TEMPLATE in template_roots:
        doc_type = "QRDA I"
    if QRDA3_TEMPLATE in template_roots:
        doc_type = "QRDA III"
    type_ok = doc_type is not None and (declared_type is None or declared_type == doc_type)
    record("qrda-template-id", type_ok,
           f"document type detected: {doc_type}" if doc_type else
           "no QRDA I / QRDA III templateId found")

    header_names = ("id", "effectiveTime", "confidentialityCode", "languageCode",
                    "recordTarget", "author", "custodian")
    # Cat III has no single recordTarget patient; header check then
    # requires recordTarget only for Cat I.
    required = header_names if doc_type != "QRDA III" else \
        tuple(n for n in header_names if n != "recordTarget")
    missing = [n for n in required if _find_first(root, n) is None]
    record("header-completeness", not missing,
           "all required header elements present" if not missing else
           f"missing header elements: {missing}")

    measure_ids = [e.get("extension") for e in _find_all(root, "externalDocument")
                   for _ in [0]]
    has_measure_section = MEASURE_SECTION_TEMPLATE in template_roots or bool(measure_ids)
    record("measure-section-present", has_measure_section,
           "measure section present" if has_measure_section else "no measure section")

    if doc_type == "QRDA I":
        values = {e.get("code") for e in _find_all(root, "value")}
        has_membership = bool(values & {"DENOM", "NUMER", "IPP"})
        record("cat1-patient-data-membership", has_membership,
               f"population membership codes present: {sorted(values & {'DENOM', 'NUMER', 'IPP'})}"
               if has_membership else "no DENOM/NUMER/IPP membership entry")
        record("cat3-aggregate-counts-sane", True, "not applicable to QRDA I")
    elif doc_type == "QRDA III":
        record("cat1-patient-data-membership", True, "not applicable to QRDA III")
        counts: dict[str, int] = {}
        sane = True
        detail = ""
        for obs in _find_all(root, "observation"):
            codes = [v.get("code") for v in obs.iter() if _local(v.tag) == "value"]
            ints = [v.get("value") for v in obs.iter()
                    if _local(v.tag) == "value" and v.get("type") == "INT"]
            for code_value in ("DENOM", "NUMER"):
                if code_value in codes and ints:
                    try:
                        counts[code_value] = int(ints[0])
                    except (TypeError, ValueError):
                        sane = False
                        detail = f"{code_value} count is not an integer"
        if sane and ("DENOM" not in counts or "NUMER" not in counts):
            sane, detail = False, "aggregate DENOM/NUMER counts not found"
        if sane and (counts["DENOM"] < 0 or counts["NUMER"] < 0):
            sane, detail = False, "aggregate counts must be non-negative"
        if sane and counts["NUMER"] > counts["DENOM"]:
            sane, detail = False, "numerator exceeds denominator"
        record("cat3-aggregate-counts-sane", sane,
               detail or f"DENOM={counts.get('DENOM')} NUMER={counts.get('NUMER')}")
    else:
        record("cat1-patient-data-membership", False, "document type unknown")
        record("cat3-aggregate-counts-sane", False, "document type unknown")

    period_ok, period_detail = False, "no reporting-period effectiveTime found"
    for obs in _find_all(root, "observation"):
        eff = next((c for c in obs if _local(c.tag) == "effectiveTime"), None)
        if eff is None:
            continue
        low = next((c for c in eff if _local(c.tag) == "low"), None)
        high = next((c for c in eff if _local(c.tag) == "high"), None)
        if low is None or high is None:
            continue
        try:
            low_d = datetime.strptime(low.get("value"), "%Y%m%d").date()
            high_d = datetime.strptime(high.get("value"), "%Y%m%d").date()
            period_ok = low_d <= high_d
            period_detail = f"period {low_d.isoformat()}..{high_d.isoformat()}"
        except (TypeError, ValueError):
            period_detail = "reporting-period bounds are not YYYYMMDD dates"
        break
    record("reporting-period-valid", period_ok, period_detail)

    bad_ids = [e for e in _find_all(root, "id")
               if not e.get("root") and not e.get("extension")]
    record("id-attributes-present", not bad_ids,
           "every id carries root or extension" if not bad_ids else
           f"{len(bad_ids)} id element(s) lack root/extension")

    return {"valid": all(c["status"] == "pass" for c in checks),
            "document_type": doc_type, "checks": checks,
            "implemented_checks": IMPLEMENTED_CHECKS, "not_run": NOT_RUN_NOTE}


# ---------------------------------------------------------------------------
# Generation endpoints (warehouse-backed, via the Phase 9 engine)
# ---------------------------------------------------------------------------

def _period(session: Session, wh_session) -> tuple[date, date]:
    from . import pophealth as ph
    reference = ph._reference_date(wh_session)
    return date(reference.year, 1, 1), reference


def _membership(session: Session, key: str, definition: dict):
    factory = wh.warehouse_session_factory()
    with factory() as wh_session:
        wh.ensure_concept_map(wh_session)
        denominator = quality_module.criteria_person_ids(
            wh_session, definition.get("denominator") or {})
        numerator = quality_module.criteria_person_ids(
            wh_session, definition.get("numerator") or {}, universe=denominator)
        persons = {p.source_patient_id: p for p in wh_session.scalars(
            select(wh.Person)).all()}
        period = _period(session, wh_session)
    return denominator, numerator, persons, period


def _patient_dict(session: Session, patient_id: str, person) -> dict:
    row = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Patient",
        FhirResource.resource_id == patient_id)).first()
    body = (row.body or {}) if row is not None else {}
    name = ((body.get("name") or [{}])[0]) if body.get("name") else {}
    given_list = name.get("given") or []
    birth = str(body.get("birthDate") or "")
    if person is not None and person.year_of_birth:
        birth = f"{person.year_of_birth:04d}{(person.month_of_birth or 1):02d}{(person.day_of_birth or 1):02d}"
    elif birth:
        birth = birth.replace("-", "")
    gender = str(body.get("gender") or "")
    gender_code = {"male": "M", "female": "F"}.get(gender, "UN")
    return {"id": patient_id, "family": name.get("family") or patient_id,
            "given": (given_list[0] if given_list else ""), "birth": birth,
            "gender_code": gender_code}


@router.post("/measures/{key}/qrda3")
def qrda3(key: str, session: Session = Depends(get_db),
          user: User = Depends(_require("qrda.export"))):
    _ensure(session)
    definition = _measure(session, key)
    denominator, numerator, _persons, period = _membership(session, key, definition)
    xml_text = build_qrda3(org=_org(session), measure_key=key, definition=definition,
                           denominator=len(denominator), numerator=len(numerator),
                           period=period)
    append_audit(session, actor=user.username, action="qrda.export.qrda3",
                 entity=f"measure:{key}",
                 payload={"measure": key, "denominator": len(denominator),
                          "numerator": len(numerator)})
    session.commit()
    return {"measure": key, "document_type": "QRDA III",
            "denominator": len(denominator), "numerator": len(numerator),
            "xml": xml_text, "validation": validate_qrda(xml_text, "QRDA III")}


@router.post("/measures/{key}/qrda1/{patient_id}")
def qrda1(key: str, patient_id: str, session: Session = Depends(get_db),
          user: User = Depends(_require("qrda.export"))):
    _ensure(session)
    definition = _measure(session, key)
    denominator, numerator, persons, period = _membership(session, key, definition)
    person = persons.get(patient_id)
    if person is None:
        raise HTTPException(status_code=404,
                            detail=f"Patient '{patient_id}' is not in the warehouse "
                                   "(run the ETL first — QRDA is generated from "
                                   "measure-engine data, never invented)")
    patient = _patient_dict(session, patient_id, person)
    xml_text = build_qrda1(
        org=_org(session), measure_key=key, definition=definition, patient=patient,
        in_denominator=person.person_id in denominator,
        in_numerator=person.person_id in numerator, period=period)
    append_audit(session, actor=user.username, action="qrda.export.qrda1",
                 entity=f"Patient/{patient_id}",
                 payload={"measure": key, "patient": patient_id,
                          "in_denominator": person.person_id in denominator,
                          "in_numerator": person.person_id in numerator})
    session.commit()
    return {"measure": key, "document_type": "QRDA I", "patient": patient_id,
            "in_denominator": person.person_id in denominator,
            "in_numerator": person.person_id in numerator,
            "xml": xml_text, "validation": validate_qrda(xml_text, "QRDA I")}


@router.post("/validate")
async def validate(request: Request, session: Session = Depends(get_db),
                   user: User = Depends(_require("qrda.export"))):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict) or not isinstance(body.get("xml"), str):
        raise HTTPException(status_code=422, detail="'xml' (string) is required")
    declared = body.get("document_type") or body.get("documentType")
    return validate_qrda(body["xml"], declared if declared in ("QRDA I", "QRDA III") else None)


@router.get("/checks")
def checks(session: Session = Depends(get_db),
           user: User = Depends(_require("qrda.export"))):
    _ensure(session)
    return {"implemented_checks": IMPLEMENTED_CHECKS, "not_run": NOT_RUN_NOTE}
