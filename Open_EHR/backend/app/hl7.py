"""HL7 v2 — the ONE shared module (Phase 8).

Phase 2 wrote a minimal ORU^R01 reader inside app.lab for analyser
ingest. Phase 8 needs builders (ADT / ORM / ORU), ACK handling and
MLLP framing as well; rather than forking a second HL7 implementation
in the interoperability module, the Phase 2 reader moved here and grew
the rest. app.lab re-exports `parse_hl7_oru` so existing imports and
tests are untouched.

Everything here is pure text transformation — no database, no config
access (callers pass the facility identity in) — so the builders are
golden-testable in isolation.

Conventions (consistent with the Phase 2 reader and its fixtures):
- Segment separator is CR in built messages; the reader accepts
  CR / LF / CRLF.
- PID-3's first repetition carries the OpenCare patient resource id
  (that is what the ingest path matches on); the MRN, when the chart
  has one, rides as the second repetition.
"""
import re
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

FIELD_SEP = "|"
ENCODING_CHARS = "^~\\&"
VERSION = "2.5.1"

# MLLP framing (HL7 minimal lower layer protocol).
MLLP_START = b"\x0b"          # VT
MLLP_END = b"\x1c\x0d"        # FS CR

LOINC_SYSTEM = "http://loinc.org"


# ---------------------------------------------------------------------------
# Reading primitives (the Phase 2 reader, unchanged semantics)
# ---------------------------------------------------------------------------

def _hl7_field(parts: list[str], index: int) -> str:
    return parts[index] if index < len(parts) else ""


def _hl7_component(value: str, component: int = 0) -> str:
    pieces = value.split("^")
    return pieces[component] if component < len(pieces) else ""


def split_segments(content: str) -> list[str]:
    """Split a raw message into segment lines (CR/LF/CRLF tolerant,
    MLLP framing stripped if present)."""
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    content = content.strip("\x0b\x1c\r\n ")
    lines = [ln.strip() for ln in
             content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
             if ln.strip()]
    return lines


def parse_segments(content: str) -> list[list[str]]:
    """Parse a message into per-segment field lists. MSH is normalised
    so index N is field MSH-(N) for every segment including MSH (the
    raw MSH line keeps the separator as its first 'field')."""
    out: list[list[str]] = []
    for line in split_segments(content):
        if line.startswith("MSH"):
            sep = line[3] if len(line) > 3 else FIELD_SEP
            parts = line.split(sep)
            out.append(["MSH", sep] + parts[1:])
        else:
            out.append(line.split(FIELD_SEP))
    return out


def segment_fields(segments: list[list[str]], name: str) -> list[str] | None:
    for seg in segments:
        if seg and seg[0] == name:
            return seg
    return None


def message_type_of(segments: list[list[str]]) -> str:
    msh = segment_fields(segments, "MSH")
    if not msh:
        return ""
    return _hl7_component(_hl7_field(msh, 9), 0) + \
        ("^" + _hl7_component(_hl7_field(msh, 9), 1)
         if _hl7_component(_hl7_field(msh, 9), 1) else "")


def control_id_of(segments: list[list[str]]) -> str:
    msh = segment_fields(segments, "MSH")
    return _hl7_field(msh, 10) if msh else ""


def patient_id_of(segments: list[list[str]]) -> str:
    pid = segment_fields(segments, "PID")
    if not pid:
        return ""
    return _hl7_component(_hl7_field(pid, 3), 0)


def parse_hl7_oru(content: str) -> tuple[str, str, list[dict]]:
    """Minimal dependency-free HL7 v2 ORU^R01 reader (Phase 2, moved
    here unchanged).

    Returns (order_id, patient_id, [{code, value, unit}]). Reads PID-3
    (patient id), OBR-2/OBR-3 (order id) and OBX-3/5/6 (code, value,
    units). Malformed input raises 422 with the parse problem — ingest
    never guesses."""
    lines = [ln.strip() for ln in
             content.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if not lines or not lines[0].startswith("MSH"):
        raise HTTPException(status_code=422,
                            detail="HL7 ingest requires a message starting with MSH")
    sep = lines[0][3] if len(lines[0]) > 3 else "|"
    patient_id = order_id = None
    results = []
    saw_obx = False
    for line in lines:
        parts = line.split(sep)
        kind = parts[0]
        if kind == "PID":
            patient_id = _hl7_component(_hl7_field(parts, 3), 0) or patient_id
        elif kind == "OBR":
            order_id = (_hl7_field(parts, 3) or _hl7_field(parts, 2) or order_id)
        elif kind == "OBX":
            saw_obx = True
            code = _hl7_component(_hl7_field(parts, 3), 0)
            raw_value = _hl7_field(parts, 5)
            # OBX-6 taken whole: units like 10^9/L legitimately contain '^'.
            unit = _hl7_field(parts, 6) or None
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail=f"OBX value '{raw_value}' for code '{code}' is not numeric")
            if not code:
                raise HTTPException(status_code=422,
                                    detail="OBX segment is missing its observation code (OBX-3)")
            results.append({"code": code, "value": value, "unit": unit})
    if not order_id:
        raise HTTPException(status_code=422,
                            detail="HL7 message has no order id (OBR-2/OBR-3)")
    if not patient_id:
        raise HTTPException(status_code=422,
                            detail="HL7 message has no patient id (PID-3)")
    if not saw_obx or not results:
        raise HTTPException(status_code=422,
                            detail="HL7 message contains no OBX result segments")
    return order_id, patient_id, results


# ---------------------------------------------------------------------------
# Building primitives
# ---------------------------------------------------------------------------

_ESCAPES = {"|": "\\F\\", "^": "\\S\\", "~": "\\R\\", "\\": "\\E\\", "&": "\\T\\"}


def esc(value) -> str:
    """HL7-escape one field value (delimiters become \\X\\ sequences)."""
    if value is None:
        return ""
    text = str(value)
    return "".join(_ESCAPES.get(ch, ch) for ch in text)


def segment(name: str, *fields) -> str:
    """One segment line: fields are 1-based in order; trailing empty
    fields are trimmed (legal HL7). Components inside a field are the
    caller's literal '^' joins of already-escaped values."""
    values = ["" if f is None else str(f) for f in fields]
    while values and values[-1] == "":
        values.pop()
    return name + ("|" + "|".join(values) if values else "")


def hl7_datetime(value) -> str:
    """ISO-8601 / date / datetime -> HL7 DTM (YYYYMMDDHHMMSS)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d%H%M%S")
    digits = re.sub(r"\D", "", str(value))
    return digits[:14].ljust(8, "0") if len(digits) >= 8 else digits


def hl7_date(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[:8]


def new_control_id(prefix: str = "MSG") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16].upper()}"


def now_dtm() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def build_msh(message_type: str, control_id: str, *,
              sending_application: str, sending_facility: str,
              receiving_application: str = "", receiving_facility: str = "",
              processing_id: str = "P") -> str:
    return segment(
        "MSH", ENCODING_CHARS, esc(sending_application), esc(sending_facility),
        esc(receiving_application), esc(receiving_facility), now_dtm(), "",
        message_type, control_id, processing_id, VERSION)


def _patient_mrn(patient_body: dict) -> str:
    for ident in patient_body.get("identifier") or []:
        if not isinstance(ident, dict):
            continue
        system = str(ident.get("system") or "").lower()
        if "mrn" in system and ident.get("value"):
            return str(ident["value"])
    for ident in patient_body.get("identifier") or []:
        if isinstance(ident, dict) and ident.get("value"):
            return str(ident["value"])
    return ""


def pid_segment(patient_body: dict) -> str:
    """PID from a FHIR Patient body. PID-3 first repetition = the
    patient resource id (the identifier this system's ingest matches
    on); the MRN, when present, is the second repetition."""
    patient_id = str(patient_body.get("id") or "")
    mrn = _patient_mrn(patient_body)
    pid3 = f"{esc(patient_id)}^^^OPENCARE^FI"
    if mrn and mrn != patient_id:
        pid3 += f"~{esc(mrn)}^^^OPENCARE^MR"
    name = (patient_body.get("name") or [{}])[0]
    family = esc(name.get("family") or "")
    given = "^".join(esc(g) for g in (name.get("given") or []))
    name_field = family + ("^" + given if given else "")
    gender = {"male": "M", "female": "F", "other": "O",
              "unknown": "U"}.get(str(patient_body.get("gender") or "").lower(), "")
    return segment("PID", "1", "", pid3, "", name_field, "",
                   hl7_date(patient_body.get("birthDate")), gender)


_CLASS_MAP = {"IMP": "I", "AMB": "O", "EMER": "E", "HH": "H", "ACUTE": "I"}


def pv1_segment(encounter_body: dict, *, assigned_location: str = "",
                prior_location: str = "", visit_number: str = "",
                discharge_datetime: str = "") -> str:
    """PV1 from a FHIR Encounter body. `assigned_location` /
    `prior_location` are pre-composed POC^Room^Bed strings (callers
    resolve bed codes through the locations config)."""
    # Encounter.class is a bare Coding ({system, code}) in FHIR R4 and
    # in this system's stored bodies; tolerate CodeableConcept too.
    class_code = ""
    class_value = encounter_body.get("class") or {}
    if isinstance(class_value, dict):
        if class_value.get("code"):
            class_code = str(class_value["code"])
        else:
            class_coding = class_value.get("coding") or []
            if class_coding:
                class_code = str(class_coding[0].get("code") or "")
    patient_class = _CLASS_MAP.get(class_code.upper(), "I" if class_code else "")
    period = encounter_body.get("period") or {}
    fields = [""] * 45
    fields[0] = "1"                       # PV1-1 set id
    fields[1] = patient_class             # PV1-2 patient class
    fields[2] = assigned_location         # PV1-3 assigned location
    fields[5] = prior_location            # PV1-6 prior location
    fields[18] = visit_number or str(encounter_body.get("id") or "")  # PV1-19
    fields[43] = hl7_datetime(period.get("start"))                    # PV1-44 admit
    fields[44] = hl7_datetime(discharge_datetime or period.get("end"))  # PV1-45
    return segment("PV1", *fields)


def _coding_of(codeable: dict | None) -> tuple[str, str, str]:
    coding = ((codeable or {}).get("coding") or [])
    if not coding:
        return "", "", ""
    first = coding[0]
    return (str(first.get("code") or ""), str(first.get("display") or ""),
            str(first.get("system") or ""))


def cwe(code: str, display: str, system: str) -> str:
    parts = [esc(code), esc(display), esc(system)]
    while parts and parts[-1] == "":
        parts.pop()
    return "^".join(parts)


def build_adt(patient_body: dict, encounter_body: dict, event_type: str, *,
              control_id: str, sending_application: str, sending_facility: str,
              receiving_application: str = "", receiving_facility: str = "",
              assigned_location: str = "", prior_location: str = "",
              discharge_datetime: str = "") -> str:
    """ADT^A01/A02/A03/A08 from an AdtEvent + the Patient/Encounter."""
    lines = [
        build_msh(f"ADT^{event_type}", control_id,
                  sending_application=sending_application,
                  sending_facility=sending_facility,
                  receiving_application=receiving_application,
                  receiving_facility=receiving_facility),
        segment("EVN", event_type, now_dtm()),
        pid_segment(patient_body),
        pv1_segment(encounter_body, assigned_location=assigned_location,
                    prior_location=prior_location,
                    discharge_datetime=discharge_datetime),
    ]
    return "\r".join(lines)


def build_orm(order_body: dict, patient_body: dict, *,
              control_id: str, sending_application: str, sending_facility: str,
              receiving_application: str = "", receiving_facility: str = "") -> str:
    """ORM^O01 for one signed CPOE order (ServiceRequest or
    MedicationRequest body)."""
    codeable = (order_body.get("code") or order_body.get("medicationCodeableConcept")
                or {})
    code, display, system = _coding_of(codeable)
    order_id = str(order_body.get("id") or "")
    authored = order_body.get("authoredOn") or order_body.get("occurrenceDateTime") or ""
    lines = [
        build_msh("ORM^O01", control_id,
                  sending_application=sending_application,
                  sending_facility=sending_facility,
                  receiving_application=receiving_application,
                  receiving_facility=receiving_facility),
        pid_segment(patient_body),
        segment("ORC", "NW", order_id, "", "", "", "", "", "",
                hl7_datetime(authored)),
        segment("OBR", "1", order_id, "", cwe(code, display, system)),
    ]
    return "\r".join(lines)


def _obx_value(observation_body: dict) -> tuple[str, str, str]:
    """(OBX-2 value type, OBX-5 value, OBX-6 units) for one
    Observation body."""
    if "valueQuantity" in observation_body:
        qty = observation_body.get("valueQuantity") or {}
        return "NM", str(qty.get("value", "")), str(qty.get("unit") or "")
    if "valueString" in observation_body:
        return "ST", esc(observation_body.get("valueString") or ""), ""
    if "valueCodeableConcept" in observation_body:
        code, display, system = _coding_of(observation_body.get("valueCodeableConcept"))
        return "CWE", cwe(code, display, system), ""
    if "valueBoolean" in observation_body:
        return "ST", "true" if observation_body.get("valueBoolean") else "false", ""
    return "ST", "", ""


def build_oru(report_body: dict, observation_bodies: list[dict],
              order_body: dict | None, patient_body: dict, *,
              control_id: str, sending_application: str, sending_facility: str,
              receiving_application: str = "", receiving_facility: str = "",
              elr_condition: str = "") -> str:
    """ORU^R01 for one released lab report. With `elr_condition` set
    the message is the public-health (ELR) variant: an NTE after the
    OBR names the reportable condition that triggered it."""
    code, display, system = _coding_of(report_body.get("code"))
    order_id = ""
    if order_body is not None:
        order_id = str(order_body.get("id") or "")
    issued = report_body.get("issued") or ""
    lines = [
        build_msh("ORU^R01", control_id,
                  sending_application=sending_application,
                  sending_facility=sending_facility,
                  receiving_application=receiving_application,
                  receiving_facility=receiving_facility),
        pid_segment(patient_body),
        segment("OBR", "1", order_id, "",
                cwe(code, display, system or LOINC_SYSTEM), "", "", "", "", "",
                "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
                hl7_datetime(issued)),
    ]
    if elr_condition:
        lines.append(segment("NTE", "1", "",
                             esc(f"Electronic laboratory report (ELR): "
                                 f"reportable condition — {elr_condition}")))
    for index, obs in enumerate(observation_bodies, start=1):
        ocode, odisplay, osystem = _coding_of(obs.get("code"))
        value_type, value, unit = _obx_value(obs)
        ref_text = ""
        ranges = obs.get("referenceRange") or []
        if ranges:
            low = (ranges[0].get("low") or {}).get("value")
            high = (ranges[0].get("high") or {}).get("value")
            if low is not None and high is not None:
                ref_text = f"{low}-{high}"
        flags = ""
        interp = ((obs.get("interpretation") or [{}])[0].get("coding") or [])
        if interp:
            flags = str(interp[0].get("code") or "")
        lines.append(segment(
            "OBX", str(index), value_type,
            cwe(ocode, odisplay, osystem or LOINC_SYSTEM), "", value,
            esc(unit), ref_text, esc(flags), "", "", "F"))
    return "\r".join(lines)


# ---------------------------------------------------------------------------
# ACKs
# ---------------------------------------------------------------------------

def build_ack(original: str, ack_code: str, text: str = "", *,
              sending_application: str = "OPENCARE",
              sending_facility: str = "OPENCARE") -> str:
    """ACK for a received message. ack_code: AA (accept), AE (error),
    AR (reject). An ERR segment rides along for AE/AR."""
    segments = []
    try:
        segments = parse_segments(original)
    except Exception:
        segments = []
    msh = segment_fields(segments, "MSH")
    original_type = message_type_of(segments) if msh else ""
    original_control = control_id_of(segments) if msh else ""
    original_sender = _hl7_field(msh, 3) if msh else ""
    original_facility = _hl7_field(msh, 4) if msh else ""
    ack_type = "ACK" + (f"^{original_type.split('^')[1]}" if "^" in original_type else "")
    lines = [
        build_msh(ack_type, new_control_id("ACK"),
                  sending_application=sending_application,
                  sending_facility=sending_facility,
                  receiving_application=original_sender,
                  receiving_facility=original_facility),
        segment("MSA", ack_code, original_control, esc(text)),
    ]
    if ack_code in ("AE", "AR"):
        lines.append(segment("ERR", "", "", "207^Application Internal Error^HL70357",
                             "E", "", esc(text)))
    return "\r".join(lines)


def parse_ack(raw: str) -> dict:
    """{ack_code, control_id, text} from an ACK message. ack_code is
    '' when the response carries no MSA (not an ACK at all)."""
    segments = parse_segments(raw)
    msa = segment_fields(segments, "MSA")
    if not msa:
        return {"ack_code": "", "control_id": "", "text": ""}
    return {"ack_code": _hl7_field(msa, 1),
            "control_id": _hl7_field(msa, 2),
            "text": _hl7_field(msa, 3)}


# ---------------------------------------------------------------------------
# MLLP framing
# ---------------------------------------------------------------------------

def wrap_mllp(message: str) -> bytes:
    return MLLP_START + message.encode("utf-8") + MLLP_END


def unwrap_mllp(data: bytes) -> str:
    """Strip one MLLP frame (tolerant: unframed input passes through)."""
    if data.startswith(MLLP_START):
        data = data[len(MLLP_START):]
    if data.endswith(MLLP_END):
        data = data[:-len(MLLP_END)]
    return data.decode("utf-8", errors="replace")


def extract_frame(buffer: bytes) -> tuple[str | None, bytes]:
    """Pull one complete frame out of a receive buffer. Returns
    (message, remaining_buffer); message is None until the FS CR
    terminator has arrived."""
    end = buffer.find(MLLP_END)
    if end == -1:
        return None, buffer
    frame = buffer[:end]
    rest = buffer[end + len(MLLP_END):]
    if frame.startswith(MLLP_START):
        frame = frame[len(MLLP_START):]
    return frame.decode("utf-8", errors="replace"), rest


# ---------------------------------------------------------------------------
# VXU builder (Phase 15, BD-33 — appended; nothing above was modified)
# ---------------------------------------------------------------------------

def build_vxu(immunization_body: dict, patient_body: dict, *,
              control_id: str, sending_application: str, sending_facility: str,
              receiving_application: str = "",
              receiving_facility: str = "") -> str:
    """VXU^V04 (unsolicited vaccination record update) for one FHIR
    Immunization body + its Patient body, in the IIS style:

    MSH / PID / ORC / RXA (+ RXR when the record carries a route or
    site). RXA-5 is the vaccine CWE with coding-system name CVX;
    RXA-15 the lot number; RXA-17 the manufacturer display; RXA-20
    the completion status (CP for a completed administration, RE for
    not-done). RXC is not emitted: the capture model records no
    vaccine components, and an RXC would have to invent them."""
    imm_id = str(immunization_body.get("id") or "")
    occurrence = immunization_body.get("occurrenceDateTime") or ""
    code, display, _system = _coding_of(immunization_body.get("vaccineCode"))
    dose = immunization_body.get("doseQuantity") or {}
    dose_value = dose.get("value")
    dose_unit = str(dose.get("unit") or dose.get("code") or "")
    manufacturer = immunization_body.get("manufacturer") or {}
    manufacturer_display = str(manufacturer.get("display") or "") \
        if isinstance(manufacturer, dict) else ""
    status = str(immunization_body.get("status") or "")
    completion = {"completed": "CP", "not-done": "RE"}.get(status, "")
    refusal = ""
    if status == "not-done":
        rcode, rdisplay, _rsys = _coding_of(
            immunization_body.get("statusReason"))
        refusal = cwe(rcode, rdisplay, "NIP002") if rcode else ""
    rxa_fields = [""] * 21
    rxa_fields[0] = "0"                            # RXA-1 give sub-id counter
    rxa_fields[1] = "1"                            # RXA-2 administration sub-id
    rxa_fields[2] = hl7_datetime(occurrence)       # RXA-3 start of administration
    rxa_fields[3] = hl7_datetime(occurrence)       # RXA-4 end of administration
    rxa_fields[4] = cwe(code, display, "CVX")      # RXA-5 administered code
    rxa_fields[5] = "" if dose_value is None else str(dose_value)  # RXA-6 amount
    rxa_fields[6] = esc(dose_unit)                 # RXA-7 units
    rxa_fields[14] = esc(immunization_body.get("lotNumber") or "")  # RXA-15 lot
    rxa_fields[15] = hl7_date(immunization_body.get("expirationDate"))  # RXA-16
    rxa_fields[16] = cwe("", manufacturer_display, "MVX") if manufacturer_display \
        else ""                                    # RXA-17 manufacturer
    rxa_fields[17] = refusal                       # RXA-18 refusal reason
    rxa_fields[19] = completion                    # RXA-20 completion status
    rxa_fields[20] = "A"                           # RXA-21 action code (add)
    lines = [
        build_msh("VXU^V04", control_id,
                  sending_application=sending_application,
                  sending_facility=sending_facility,
                  receiving_application=receiving_application,
                  receiving_facility=receiving_facility),
        pid_segment(patient_body),
        segment("ORC", "RE", imm_id, imm_id),
        segment("RXA", *rxa_fields),
    ]
    route_code, route_display, _rs = _coding_of(immunization_body.get("route"))
    site_code, site_display, _ss = _coding_of(immunization_body.get("site"))
    if route_code or site_code:
        lines.append(segment(
            "RXR",
            cwe(route_code, route_display, "HL70162") if route_code else "",
            cwe(site_code, site_display, "HL70163") if site_code else ""))
    return "\r".join(lines)
