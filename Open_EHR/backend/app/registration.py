"""Patient registration & the front desk — Front Door pass
(app.registration).

Registration is real, validated master-data work, not a bare FHIR
POST with the caller's fingers crossed:

- **MRN from published config.** The medical record number's system,
  prefix, separator and zero-padded width live in the published
  `integration/registration` config (kind=integration — a site
  republishes its own format; no code changes). The sequence itself
  is data: next = highest existing MRN number + 1. Manual
  identifiers can be added alongside; a manual identifier in the
  MRN system replaces the generated one.
- **Duplicate safety.** Registering a patient whose family + given
  name and date of birth match an existing record returns 409 WITH
  the candidate matches; creation proceeds only when the caller
  explicitly confirms (`confirm_duplicate: true`). A pre-check
  endpoint exposes the same matching for the form's live warning.
- **Typed validation** (app.validate_fields + this module): names
  required, birthDate a real calendar date and not in the future,
  gender in the FHIR value set, email/phone format-checked,
  emergency-contact phone format-checked. Every failure is a 422
  naming the field — the form shows it inline, nothing fails
  silently.

The front-desk "today" endpoint feeds the seeded front-desk
audience dashboard: today's appointments with status, arrivals
awaiting check-in (booked, not yet arrived) and awaiting a room
(arrived, not yet roomed) computed from the live Appointment
resources and the Phase 12 rooming extension, the open waitlist
size, and how many patients were registered today.
"""
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir, people, validate_fields as vf
from .audit import append_audit
from .configstore import published_config
from .models import FhirResource, User, WaitlistEntry
from .security import get_db, require_capability

router = APIRouter(prefix="/registration", tags=["registration"])

REGISTRATION_KEY = "registration"
GENDERS = ("male", "female", "other", "unknown")
OPEN_WAITLIST_STATUSES = ("waiting", "offered")


def default_registration_payload() -> dict:
    return {
        "mrn": {"system": "urn:opencare:mrn", "prefix": "MRN",
                "separator": "-", "width": 6},
        "duplicateCheck": {"matchOn": ["family", "given", "birthDate"]},
    }


def _ops(session: Session) -> dict:
    cfg = published_config(session, "integration", REGISTRATION_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    defaults = default_registration_payload()
    return {**defaults, **payload,
            "mrn": {**defaults["mrn"], **(payload.get("mrn") or {})}}


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
# Patient summaries + duplicate matching
# ---------------------------------------------------------------------------

def _name_parts(body: dict) -> tuple[str, str]:
    for name in body.get("name") or []:
        if isinstance(name, dict):
            given = name.get("given") or []
            first = given[0] if given and isinstance(given[0], str) else ""
            return (name.get("family") or ""), first
    return "", ""


def patient_display_name(body: dict) -> str:
    family, given = _name_parts(body)
    return " ".join(part for part in (given, family) if part) or "(unnamed)"


def _patient_summary(row: FhirResource) -> dict:
    body = row.body or {}
    return {"id": row.resource_id,
            "name": patient_display_name(body),
            "birthDate": body.get("birthDate"),
            "gender": body.get("gender"),
            "active": body.get("active") is not False,
            "identifiers": [i for i in body.get("identifier") or []
                            if isinstance(i, dict)]}


def find_duplicates(session: Session, family: str, given: str,
                    birth_date: str) -> list[dict]:
    """Exact-match duplicate candidates: same family + given name
    (case/whitespace-insensitive) AND same date of birth."""
    fam = family.strip().casefold()
    giv = given.strip().casefold()
    matches = []
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Patient")).all():
        body = row.body or {}
        if body.get("birthDate") != birth_date:
            continue
        other_family, other_given = _name_parts(body)
        if other_family.strip().casefold() == fam \
                and other_given.strip().casefold() == giv:
            matches.append(_patient_summary(row))
    return matches


# ---------------------------------------------------------------------------
# MRN generation (format from published config; sequence from data)
# ---------------------------------------------------------------------------

def _next_mrn(session: Session, mrn_cfg: dict) -> str:
    system = mrn_cfg.get("system") or "urn:opencare:mrn"
    prefix = mrn_cfg.get("prefix") or "MRN"
    separator = mrn_cfg.get("separator")
    separator = separator if isinstance(separator, str) else "-"
    width = mrn_cfg.get("width")
    width = width if isinstance(width, int) and 1 <= width <= 12 else 6
    head = f"{prefix}{separator}"
    highest = 0
    for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Patient")).all():
        for ident in (row.body or {}).get("identifier") or []:
            if not isinstance(ident, dict) or ident.get("system") != system:
                continue
            value = ident.get("value") or ""
            if value.startswith(head) and value[len(head):].isdigit():
                highest = max(highest, int(value[len(head):]))
    return f"{head}{highest + 1:0{width}d}"


# ---------------------------------------------------------------------------
# Registration endpoints
# ---------------------------------------------------------------------------

@router.post("/patients/check-duplicates")
async def check_duplicates(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("registration.work"))):
    """Pre-check for the registration form's live duplicate warning:
    the candidate records matching name + date of birth."""
    body = await _json_body(request)
    family = vf.require_str(body, "family")
    given = vf.require_str(body, "given")
    birth_date = vf.check_iso_date(body.get("birthDate"), "birthDate",
                                   required=True)
    candidates = find_duplicates(session, family, given, birth_date)
    return {"candidates": candidates, "total": len(candidates)}


REGISTRATION_LOCATION_EXTENSION = "urn:opencare:registration-location"


def _validate_registration(body: dict, session: Session | None = None) -> dict:
    family = vf.require_str(body, "family")
    given = vf.require_str(body, "given")
    birth_date = vf.check_iso_date(body.get("birthDate"), "birthDate",
                                   required=True)
    if birth_date > date.today().isoformat():
        raise HTTPException(
            status_code=422,
            detail="'birthDate' cannot be in the future")
    gender = body.get("gender")
    if gender is not None and gender not in GENDERS:
        raise HTTPException(
            status_code=422,
            detail=f"'gender' must be one of {', '.join(GENDERS)}")
    phone = vf.check_phone(vf.optional_str(body, "phone"))
    email = vf.check_email(vf.optional_str(body, "email"))
    identifiers = body.get("identifiers") or []
    if not isinstance(identifiers, list):
        raise HTTPException(status_code=422,
                            detail="'identifiers' must be a list of "
                                   "{system, value} objects")
    clean_identifiers = []
    for index, ident in enumerate(identifiers):
        if not isinstance(ident, dict) \
                or not isinstance(ident.get("system"), str) \
                or not ident.get("system", "").strip() \
                or not isinstance(ident.get("value"), str) \
                or not ident.get("value", "").strip():
            raise HTTPException(
                status_code=422,
                detail=f"'identifiers[{index}]' must carry a non-empty "
                       "'system' and 'value'")
        clean_identifiers.append({"system": ident["system"].strip(),
                                  "value": ident["value"].strip()})
    address = body.get("address") or {}
    if not isinstance(address, dict):
        raise HTTPException(status_code=422,
                            detail="'address' must be an object")
    clean_address = {}
    for field in ("line", "city", "state", "postalCode", "country"):
        value = address.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise HTTPException(
                    status_code=422,
                    detail=f"'address.{field}' must be a string")
            if value.strip():
                clean_address[field] = value.strip()
    contact = body.get("emergency_contact") or {}
    if not isinstance(contact, dict):
        raise HTTPException(status_code=422,
                            detail="'emergency_contact' must be an object")
    contact_name = vf.optional_str(contact, "name")
    contact_phone = vf.check_phone(vf.optional_str(contact, "phone"),
                                   field="emergency_contact.phone")
    if contact_phone and not contact_name:
        raise HTTPException(
            status_code=422,
            detail="'emergency_contact.name' is required when an "
                   "emergency contact phone is given")
    confirm = body.get("confirm_duplicate")
    if confirm is not None and not isinstance(confirm, bool):
        raise HTTPException(status_code=422,
                            detail="'confirm_duplicate' must be true or false")
    # Organisation placement (optional): the facility where the
    # patient registered must be an ACTIVE facility-type concept in
    # the published locations hierarchy — anything else is a 422
    # naming registration_location. Absent -> unchanged behaviour.
    registration_location = body.get("registration_location")
    if registration_location is not None:
        if not isinstance(registration_location, str) \
                or not registration_location.strip():
            raise HTTPException(
                status_code=422,
                detail="'registration_location' must be a non-empty "
                       "string (a facility code from the published "
                       "locations hierarchy)")
        registration_location = registration_location.strip()
        if session is not None \
                and people._concept_level(session, registration_location) \
                != "location":
            raise HTTPException(
                status_code=422,
                detail=f"Unknown registration_location "
                       f"'{registration_location}': not an active "
                       "facility in the published locations hierarchy")
    return {"family": family, "given": given, "birth_date": birth_date,
            "gender": gender, "phone": phone, "email": email,
            "identifiers": clean_identifiers, "address": clean_address,
            "contact_name": contact_name, "contact_phone": contact_phone,
            "confirm_duplicate": bool(confirm),
            "registration_location": registration_location}


@router.post("/patients", status_code=201)
async def register_patient(request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("registration.work"))):
    """Register a patient: validate, duplicate-check, assign the
    next MRN from the published format, and create the FHIR Patient
    through the ordinary governed create (which audits the write).
    A name+DOB match without explicit confirmation is a 409 whose
    body carries the candidates — never a silent double-register."""
    body = await _json_body(request)
    data = _validate_registration(body, session)
    candidates = find_duplicates(session, data["family"], data["given"],
                                   data["birth_date"])
    if candidates and not data["confirm_duplicate"]:
        return JSONResponse(
            status_code=409,
            content={"detail": "A patient with this name and date of "
                               "birth already exists. Review the "
                               "candidates; resubmit with "
                               "confirm_duplicate=true to register anyway.",
                     "candidates": candidates})
    ops = _ops(session)
    mrn_cfg = ops["mrn"]
    identifiers = list(data["identifiers"])
    mrn_value = None
    if not any(i["system"] == mrn_cfg["system"] for i in identifiers):
        mrn_value = _next_mrn(session, mrn_cfg)
        identifiers.insert(0, {"system": mrn_cfg["system"],
                               "value": mrn_value,
                               "use": "official"})
    else:
        mrn_value = next(i["value"] for i in identifiers
                         if i["system"] == mrn_cfg["system"])
    patient_body: dict = {
        "resourceType": "Patient",
        "active": True,
        "name": [{"use": "official", "family": data["family"],
                  "given": [data["given"]]}],
        "birthDate": data["birth_date"],
        "identifier": identifiers,
    }
    if data["gender"]:
        patient_body["gender"] = data["gender"]
    telecom = []
    if data["phone"]:
        telecom.append({"system": "phone", "value": data["phone"],
                        "use": "mobile"})
    if data["email"]:
        telecom.append({"system": "email", "value": data["email"]})
    if telecom:
        patient_body["telecom"] = telecom
    if data["address"]:
        address = dict(data["address"])
        if "line" in address:
            address["line"] = [address["line"]]
        address["use"] = "home"
        patient_body["address"] = [address]
    if data["contact_name"]:
        contact: dict = {"name": {"text": data["contact_name"]},
                         "relationship": [{"text": "Emergency contact"}]}
        if data["contact_phone"]:
            contact["telecom"] = [{"system": "phone",
                                   "value": data["contact_phone"]}]
        patient_body["contact"] = [contact]
    if data["registration_location"]:
        patient_body["extension"] = [{
            "url": REGISTRATION_LOCATION_EXTENSION,
            "valueString": data["registration_location"]}]
    created = fhir.create_resource("Patient", patient_body, session, user)
    append_audit(session, actor=user.username,
                 action="registration.patient.create",
                 entity=f"Patient/{created['id']}",
                 payload={"mrn": mrn_value,
                          "duplicates_confirmed": bool(candidates),
                          **({"registration_location":
                              data["registration_location"]}
                             if data["registration_location"] else {})})
    session.commit()
    return {**created, "mrn": mrn_value,
            "duplicates_confirmed": bool(candidates),
            **({"registration_location": data["registration_location"]}
               if data["registration_location"] else {})}


# ---------------------------------------------------------------------------
# Front-desk today — the audience dashboard's data
# ---------------------------------------------------------------------------

@router.get("/frontdesk/today")
def frontdesk_today(session: Session = Depends(get_db),
                    user: User = Depends(require_capability("registration.work"))):
    """Today at the front desk, from live data: appointments with
    status, arrivals awaiting check-in (status booked — expected,
    not yet arrived) and awaiting a room (arrived/checked-in with no
    rooming recorded), the open waitlist, and today's registrations."""
    from . import boards, continuum
    today = _now().date()
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Appointment")).all()
    names: dict[str, str] = {}
    items = []
    by_status: dict[str, int] = {}
    awaiting_checkin = awaiting_room = 0
    for row in rows:
        if row.start_at is None or row.start_at.date() != today:
            continue
        body = row.body or {}
        patient_ref = None
        for participant in body.get("participant") or []:
            if isinstance(participant, dict):
                actor = participant.get("actor") or {}
                ref = actor.get("reference") if isinstance(actor, dict) else None
                if ref and ref.startswith("Patient/"):
                    patient_ref = ref
                    break
        if patient_ref and patient_ref not in names:
            prow = session.scalars(select(FhirResource).where(
                FhirResource.resource_type == "Patient",
                FhirResource.resource_id == patient_ref.split("/", 1)[1])).first()
            names[patient_ref] = (patient_display_name(prow.body or {})
                                  if prow is not None else patient_ref)
        status = row.status or body.get("status")
        by_status[status] = by_status.get(status, 0) + 1
        rooming = boards._get_extension(body, continuum.ROOMING_EXTENSION)
        if status == "booked":
            awaiting_checkin += 1
        elif status in ("arrived", "checked-in") and rooming is None:
            awaiting_room += 1
        session_ext = boards._get_extension(body, continuum.SESSION_EXTENSION)
        items.append({
            "appointment_id": row.resource_id,
            "patient_ref": patient_ref,
            "patient": names.get(patient_ref) if patient_ref else None,
            "start": row.start_at.isoformat() if row.start_at else None,
            "time": row.start_at.strftime("%H:%M") if row.start_at else None,
            "status": status,
            "session": session_ext.get("valueString") if session_ext else None,
            "roomed": rooming is not None,
        })
    items.sort(key=lambda i: i["start"] or "")
    waitlist_open = len(session.scalars(select(WaitlistEntry).where(
        WaitlistEntry.status.in_(OPEN_WAITLIST_STATUSES))).all())
    registrations_today = sum(
        1 for row in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Patient")).all()
        if row.created_at is not None and row.created_at.date() == today)
    return {"date": today.isoformat(),
            "appointments_total": len(items),
            "by_status": by_status,
            "awaiting_checkin": awaiting_checkin,
            "awaiting_room": awaiting_room,
            "waitlist_open": waitlist_open,
            "registrations_today": registrations_today,
            "items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    from .configstore import list_config_versions
    from .models import ConfigStatus, StudioConfig
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


def ensure_registration_defaults(session: Session) -> None:
    """Seed the published registration config (MRN format + duplicate
    policy). Idempotent."""
    _seed_published(session, "integration", REGISTRATION_KEY,
                    default_registration_payload())
    session.commit()
