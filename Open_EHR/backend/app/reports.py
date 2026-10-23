"""Reports, documents, export & import — Phase 13, Track F (app.reports).

The data platform's governed surface:

- **Report catalogue**: report definitions are Studio config (kind
  `report`) naming a SERVER-DEFINED query from REPORT_QUERIES — a
  report never carries SQL. A run validates its parameters against
  the definition's declared schema, executes the named query over
  live module data, and renders PDF (ReportLab — the open-source
  library that installs cleanly in the project venv; WeasyPrint was
  rejected because it needs system Pango/Cairo libraries the
  deployment image does not carry) or CSV. Every run is audited with
  its parameters and row count.
- **Print documents**: invoice (per claim), payment receipt (per
  recorded payment), patient statement (per patient) and superbill
  (per coded encounter), rendered server-side as PDFs from live
  billing data. The letterhead — org name, address lines, footer and
  the confidentiality notice — is published config
  (integration/print), never code. EVERY document generation is
  audited (`reports.document.print`: who printed what, when) — the
  Track K print-confidentiality control.
- **Export centre**: named, server-defined datasets (patients,
  appointments, orders, lab results, billing lines) exported as CSV
  or JSON behind the `data.export` capability. No ad-hoc SQL exists
  anywhere in this surface. Exports above the published row
  threshold (integration/exports) require a recorded reason; every
  export is audited with its row count.
- **Import centre**: mapping profiles are Studio config (kind
  `mapping`: source column -> target field, transforms limited to a
  governed set). An import runs a full validation pass first —
  every row, every field, a complete error report — and the commit
  is ALL-OR-NOTHING, the Phase 7 835-poster rule: any error,
  nothing imports.
"""
import csv
import io
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import billing, fhir, org_hierarchy, pharmacy, pharmacy_ops, revenue
from .audit import append_audit
from .configstore import (DEFAULT_ADT_TRANSITIONS, list_config_versions,
                          published_config, published_configs)
from .models import (DscsaLedgerEntry, FhirResource, PatientPayment,
                     PharmacyLot, SafetyEvent, StaffCredential, StaffMember,
                     User)
from .people import (_credential_status, _days_to_expiry, _ops as _workforce_ops,
                     _staff_names)
from .security import get_db, require_capability

router = APIRouter(prefix="/reports", tags=["reports"])

PRINT_KEY = "print"
EXPORTS_KEY = "exports"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value) -> float:
    return revenue._money(value)


# ---------------------------------------------------------------------------
# Print template + export policy config (published integration configs)
# ---------------------------------------------------------------------------

def default_print_payload() -> dict:
    return {
        "documents": {
            "orgName": "OpenCare General Hospital",
            "addressLines": ["100 Synthetic Way, Suite 200",
                             "Springfield"],
            "footer": ("OpenCare — open-source EHR. Documents generated "
                       "from live records at print time."),
            "confidentialityNotice": (
                "CONFIDENTIAL — contains protected health information. "
                "If you received this document in error, notify the "
                "privacy office and destroy it."),
        }
    }


def default_exports_payload() -> dict:
    return {"reasonRequiredAboveRows": 500}


def _print_template(session: Session) -> dict:
    cfg = published_config(session, "integration", PRINT_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    documents = payload.get("documents")
    if isinstance(documents, dict) and documents.get("orgName"):
        return documents
    return default_print_payload()["documents"]


def _export_threshold(session: Session) -> int:
    cfg = published_config(session, "integration", EXPORTS_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    value = payload.get("reasonRequiredAboveRows")
    return int(value) if isinstance(value, int) and value >= 0 else 500


# ---------------------------------------------------------------------------
# PDF rendering (ReportLab platypus)
# ---------------------------------------------------------------------------

def build_pdf(template: dict, title: str, meta: list[tuple[str, str]],
              columns: list[str] | None, rows: list[list[str]] | None,
              notes: list[str]) -> bytes:
    """One letterhead document: org header, title, meta lines, an
    optional table, closing notes, and the confidentiality notice +
    footer on EVERY page (the print-confidentiality surface)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    footer_text = str(template.get("footer") or "")
    notice = str(template.get("confidentialityNotice") or "")

    def _on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(0.75 * inch, 0.55 * inch, footer_text[:110])
        canvas.drawRightString(LETTER[0] - 0.75 * inch, 0.55 * inch,
                               f"Page {doc.page}")
        if notice:
            canvas.setFont("Helvetica-Oblique", 7)
            canvas.drawString(0.75 * inch, 0.42 * inch, notice[:130])
        canvas.restoreState()

    doc = SimpleDocTemplate(buffer, pagesize=LETTER,
                            leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.75 * inch,
                            title=title)
    story = [Paragraph(str(template.get("orgName") or "OpenCare"),
                       styles["Title"])]
    for line in template.get("addressLines") or []:
        story.append(Paragraph(str(line), styles["Normal"]))
    story.append(Spacer(1, 0.18 * inch))
    story.append(Paragraph(title, styles["Heading1"]))
    for key, value in meta:
        story.append(Paragraph(f"<b>{key}:</b> {value}", styles["Normal"]))
    story.append(Spacer(1, 0.12 * inch))
    if columns and rows is not None:
        data = [columns] + rows
        table = Table(data, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef0")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b9c4c9")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(table)
        story.append(Spacer(1, 0.12 * inch))
    for note in notes:
        story.append(Paragraph(note, styles["Normal"]))
    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    return buffer.getvalue()


def _pdf_response(template: dict, title: str, filename: str,
                  meta: list[tuple[str, str]], columns, rows,
                  notes: list[str]) -> Response:
    content = build_pdf(template, title, meta, columns, rows, notes)
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})


def _csv_text(columns: list[dict], rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([c["title"] for c in columns])
    for row in rows:
        writer.writerow([row.get(c["field"]) for c in columns])
    return buffer.getvalue()


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _money_str(value) -> str:
    return f"${_money(value or 0):,.2f}"


# ---------------------------------------------------------------------------
# Server-defined report queries — the ONLY data a report can reach.
# Each returns (columns, rows): columns [{field, title}], rows [dict].
# ---------------------------------------------------------------------------

def _q_census_by_unit(session: Session, params: dict):
    from .people import _bed_stats
    stats = _bed_stats(session)
    from . import inpatient
    units, _beds = inpatient._location_concepts(session)
    displays = {u["code"]: u.get("display") for u in units}
    columns = [{"field": "unit", "title": "Unit"},
               {"field": "beds_total", "title": "Beds"},
               {"field": "occupied", "title": "Occupied"},
               {"field": "available", "title": "Available"},
               {"field": "cleaning", "title": "Cleaning"},
               {"field": "blocked", "title": "Blocked"},
               {"field": "occupancy_pct", "title": "Occupancy %"}]
    rows = [{"unit": displays.get(code) or code, **bucket}
            for code, bucket in sorted(stats.items()) if code]
    return columns, rows


def _q_claims_summary(session: Session, params: dict):
    rows = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Claim").order_by(
        FhirResource.created_at.asc())).all()
    by_status: dict[str, dict] = {}
    for row in rows:
        body = row.body or {}
        status = body.get("status") or "unknown"
        bucket = by_status.setdefault(status, {
            "status": status, "claims": 0, "billed": 0.0, "paid": 0.0,
            "adjustments": 0.0, "patient_responsibility": 0.0,
            "balance": 0.0})
        financials = revenue.claim_financials(session, row.resource_id, body)
        bucket["claims"] += 1
        for key in ("billed", "paid", "adjustments",
                    "patient_responsibility", "balance"):
            source = {"billed": "total"}.get(key, key)
            bucket[key] = _money(bucket[key] + financials[source])
    columns = [{"field": "status", "title": "Claim status"},
               {"field": "claims", "title": "Claims"},
               {"field": "billed", "title": "Billed"},
               {"field": "paid", "title": "Paid"},
               {"field": "adjustments", "title": "Adjustments"},
               {"field": "patient_responsibility",
                "title": "Patient responsibility"},
               {"field": "balance", "title": "Balance"}]
    return columns, [by_status[k] for k in sorted(by_status)]


def _q_credential_expiry(session: Session, params: dict):
    from .people import _expiry_windows
    ops = _workforce_ops(session)
    worklist_window, _alert = _expiry_windows(ops)
    window = params.get("window_days")
    names = _staff_names(session)
    # Org placement scope (Organisation & Registration Placement
    # pass): this is the one staff-based report, and its run surface
    # accepts declared parameters — so region_id / location_id /
    # department_id / unit filter rows by the credential holder's
    # StaffMember placement (the region/location/department columns
    # + unit). The ids are first validated through
    # app.org_hierarchy.resolve_scope, so an unknown id or a broken
    # chain is the same 422 the rest of the pass raises. Staff with
    # no directory row, or a placement that does not match every
    # supplied level, are out of scope.
    region_id = params.get("region_id")
    location_id = params.get("location_id")
    department_id = params.get("department_id")
    unit = params.get("unit")
    staff_by_id = None
    if any(value is not None for value in
           (region_id, location_id, department_id, unit)):
        org_hierarchy.resolve_scope(session, region_id, location_id,
                                    department_id, unit)
        staff_by_id = {row.staff_id: row for row in
                       session.scalars(select(StaffMember)).all()}

    def _in_scope(staff_id: str) -> bool:
        if staff_by_id is None:
            return True
        staff = staff_by_id.get(staff_id)
        if staff is None:
            return False
        return ((region_id is None or staff.region == region_id)
                and (location_id is None or staff.location == location_id)
                and (department_id is None
                     or staff.department == department_id)
                and (unit is None or staff.unit == unit))

    rows = []
    for cred in session.scalars(select(StaffCredential).order_by(
            StaffCredential.expires_on.asc())).all():
        if not _in_scope(cred.staff_id):
            continue
        days = _days_to_expiry(cred.expires_on)
        if window is not None and (days is None or days > int(window)):
            continue
        rows.append({"staff": names.get(cred.staff_id),
                     "type": cred.type, "number": cred.number,
                     "expires_on": cred.expires_on, "days_to_expiry": days,
                     "status": _credential_status(days, worklist_window)})
    columns = [{"field": "staff", "title": "Staff member"},
               {"field": "type", "title": "Credential"},
               {"field": "number", "title": "Number"},
               {"field": "expires_on", "title": "Expires"},
               {"field": "days_to_expiry", "title": "Days left"},
               {"field": "status", "title": "Status"}]
    return columns, rows


def _q_safety_events(session: Session, params: dict):
    status_filter = params.get("status") or "all"
    rows = []
    for event in session.scalars(select(SafetyEvent).order_by(
            SafetyEvent.created_at.asc())).all():
        if status_filter == "open" and event.status == "closed":
            continue
        if status_filter == "closed" and event.status != "closed":
            continue
        rows.append({
            "event_id": event.event_id,
            "reported": event.created_at.date().isoformat()
            if event.created_at else "",
            "unit": event.unit or "", "category": event.category,
            "harm_level": event.harm_level, "status": event.status,
            "rca_required": "yes" if event.rca_required else "no"})
    columns = [{"field": "event_id", "title": "Event"},
               {"field": "reported", "title": "Reported"},
               {"field": "unit", "title": "Unit"},
               {"field": "category", "title": "Category"},
               {"field": "harm_level", "title": "Harm"},
               {"field": "status", "title": "Status"},
               {"field": "rca_required", "title": "RCA"}]
    return columns, rows


def _q_payments_summary(session: Session, params: dict):
    by_method: dict[str, dict] = {}
    for payment in session.scalars(select(PatientPayment).order_by(
            PatientPayment.created_at.asc())).all():
        bucket = by_method.setdefault(payment.method, {
            "method": payment.method, "payments": 0, "total": 0.0})
        bucket["payments"] += 1
        bucket["total"] = _money(bucket["total"] + payment.amount)
    columns = [{"field": "method", "title": "Method"},
               {"field": "payments", "title": "Payments"},
               {"field": "total", "title": "Total received"}]
    return columns, [by_method[k] for k in sorted(by_method)]


REPORT_QUERIES = {
    "census-by-unit": ("Census by unit", _q_census_by_unit),
    "claims-summary": ("Claims summary by status", _q_claims_summary),
    "credential-expiry": ("Credential expiry", _q_credential_expiry),
    "safety-events": ("Safety events", _q_safety_events),
    "payments-summary": ("Patient payments by method", _q_payments_summary),
}


# ---------------------------------------------------------------------------
# Report catalogue
# ---------------------------------------------------------------------------

async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


@router.get("/catalogue")
def catalogue(session: Session = Depends(get_db),
              user: User = Depends(require_capability("reports.run"))):
    """The governed report catalogue: every PUBLISHED report config
    with its parameter schema and allowed formats."""
    items = []
    for cfg in published_configs(session, "report"):
        payload = cfg.payload or {}
        items.append({"key": cfg.key, "title": payload.get("title"),
                      "description": payload.get("description"),
                      "source": payload.get("source"),
                      "parameters": payload.get("parameters") or [],
                      "formats": payload.get("formats") or ["pdf", "csv"],
                      "version": cfg.version})
    return {"items": items, "total": len(items)}


def _validate_run_params(definition: dict, supplied: dict) -> dict:
    """Validate caller parameters against the report definition's
    declared schema. Unknown parameters are refused (a report takes
    exactly what its definition declares)."""
    declared = {p["name"]: p for p in definition.get("parameters") or []
                if isinstance(p, dict) and p.get("name")}
    for name in supplied:
        if name not in declared:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown parameter '{name}' for this report; "
                       f"declared: {sorted(declared)}")
    values: dict = {}
    for name, spec in declared.items():
        value = supplied.get(name, spec.get("default"))
        if value is None:
            if spec.get("required"):
                raise HTTPException(
                    status_code=422,
                    detail=f"Parameter '{name}' is required")
            continue
        ptype = spec.get("type")
        if ptype == "date":
            try:
                date.fromisoformat(str(value))
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"Parameter '{name}' must be an ISO date")
        elif ptype == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(
                    status_code=422,
                    detail=f"Parameter '{name}' must be a number")
        elif ptype == "choice":
            if value not in (spec.get("choices") or []):
                raise HTTPException(
                    status_code=422,
                    detail=f"Parameter '{name}' must be one of "
                           f"{spec.get('choices')}")
        elif not isinstance(value, str):
            raise HTTPException(
                status_code=422,
                detail=f"Parameter '{name}' must be a string")
        values[name] = value
    return values


@router.post("/{key}/run")
async def run_report(key: str, request: Request,
                     session: Session = Depends(get_db),
                     user: User = Depends(require_capability("reports.run"))):
    cfg = published_config(session, "report", key)
    if cfg is None:
        raise HTTPException(status_code=404,
                            detail=f"No published report config '{key}'")
    definition = cfg.payload or {}
    body = await _json_body(request)
    formats = definition.get("formats") or ["pdf", "csv"]
    fmt = body.get("format") or formats[0]
    if fmt not in formats:
        raise HTTPException(
            status_code=422,
            detail=f"Format '{fmt}' is not allowed for this report "
                   f"(allowed: {formats})")
    params = _validate_run_params(definition, body.get("params") or {})
    source = definition.get("source")
    query = REPORT_QUERIES.get(source)
    if query is None:  # validator keeps this unreachable
        raise HTTPException(status_code=422,
                            detail=f"Unknown report source '{source}'")
    _title, fn = query
    columns, rows = fn(session, params)
    append_audit(session, actor=user.username, action="reports.run",
                 entity=f"report:{key}@v{cfg.version}",
                 payload={"source": source, "format": fmt,
                          "rows": len(rows), "params": params})
    session.commit()
    template = _print_template(session)
    if fmt == "csv":
        return PlainTextResponse(
            _csv_text(columns, rows), media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="{key}.csv"',
                     "X-Report-Rows": str(len(rows))})
    meta = [("Generated", _now().isoformat()),
            ("Generated by", user.username),
            ("Source", source),
            ("Rows", str(len(rows)))]
    if params:
        meta.append(("Parameters", ", ".join(
            f"{k}={v}" for k, v in sorted(params.items()))))
    table_rows = [[_fmt(row.get(c["field"])) for c in columns] for row in rows]
    return _pdf_response(template, definition.get("title") or key,
                         f"{key}.pdf", meta,
                         [c["title"] for c in columns], table_rows, [])


# ---------------------------------------------------------------------------
# Print documents — invoice / receipt / statement / superbill.
# Every generation writes a `reports.document.print` audit row
# (Track K print confidentiality: who printed what, when).
# ---------------------------------------------------------------------------

def _patient_display(session: Session, patient_ref: str | None) -> str:
    if not patient_ref:
        return ""
    patient_id = patient_ref.split("/", 1)[-1]
    row = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "Patient",
        FhirResource.resource_id == patient_id)).first()
    if row is None:
        return patient_ref
    name = ((row.body or {}).get("name") or [{}])[0]
    return name.get("text") or patient_ref


def _audit_print(session: Session, user: User, document: str, ref: str) -> None:
    append_audit(session, actor=user.username,
                 action="reports.document.print",
                 entity=f"document:{document}",
                 payload={"document": document, "reference": ref,
                          "format": "pdf"})
    session.commit()


def _coding_of(item: dict) -> dict:
    coding = ((item.get("productOrService") or {}).get("coding") or [{}])
    return coding[0] if coding else {}


@router.get("/documents/invoice/{claim_id}")
def document_invoice(claim_id: str, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("reports.run"))):
    """Invoice for one claim: its charge lines as billed, with the
    claim's posted financial position (paid / adjustments / patient
    responsibility / balance) from the remittance record."""
    row = fhir._get_row("Claim", claim_id, session)
    body = row.body or {}
    patient_ref = (body.get("patient") or {}).get("reference")
    financials = revenue.claim_financials(session, claim_id, body)
    lines = []
    for item in body.get("item") or []:
        coding = _coding_of(item)
        unit_price = (item.get("unitPrice") or {}).get("value")
        net = (item.get("net") or {}).get("value")
        quantity = item.get("quantity")
        quantity = quantity.get("value") if isinstance(quantity, dict) \
            else quantity
        lines.append([str(item.get("sequence") or ""),
                      str(coding.get("code") or ""),
                      str(coding.get("display") or ""),
                      _fmt(quantity), _money_str(unit_price),
                      _money_str(net)])
    meta = [("Claim", claim_id),
            ("Patient", _patient_display(session, patient_ref)),
            ("Status", str(body.get("status") or "")),
            ("Created", str(body.get("created") or "")),
            ("Printed", _now().isoformat()),
            ("Printed by", user.username)]
    notes = [f"Billed total: {_money_str(financials['total'])}",
             f"Insurance paid: {_money_str(financials['paid'])}",
             f"Adjustments: {_money_str(financials['adjustments'])}",
             f"Patient responsibility: "
             f"{_money_str(financials['patient_responsibility'])}",
             f"Patient paid: {_money_str(financials['patient_paid'])}",
             f"<b>Balance due: {_money_str(financials['balance'])}</b>"]
    _audit_print(session, user, "invoice", f"Claim/{claim_id}")
    return _pdf_response(_print_template(session), "Invoice",
                         f"invoice-{claim_id}.pdf", meta,
                         ["#", "Code", "Description", "Qty", "Unit price",
                          "Amount"], lines, notes)


@router.get("/documents/receipt/{payment_id}")
def document_receipt(payment_id: str, session: Session = Depends(get_db),
                     user: User = Depends(require_capability("reports.run"))):
    """Receipt for one recorded patient payment (Phase 7 payments are
    ledger records — no payment gateway exists, and the receipt says
    so via the module disclaimer)."""
    payment = session.scalars(select(PatientPayment).where(
        PatientPayment.payment_id == payment_id)).first()
    if payment is None:
        raise HTTPException(status_code=404,
                            detail=f"Payment '{payment_id}' not found")
    meta = [("Payment", payment.payment_id),
            ("Claim", payment.claim_id),
            ("Patient", _patient_display(session, payment.patient_ref)),
            ("Method", payment.method),
            ("Recorded by", payment.recorded_by),
            ("Recorded at", payment.created_at.isoformat()
             if payment.created_at else ""),
            ("Printed", _now().isoformat()),
            ("Printed by", user.username)]
    notes = [f"<b>Amount received: {_money_str(payment.amount)}</b>",
             revenue.PAYMENT_DISCLAIMER]
    _audit_print(session, user, "receipt", f"Payment/{payment_id}")
    return _pdf_response(_print_template(session), "Payment receipt",
                         f"receipt-{payment_id}.pdf", meta, None, None, notes)


@router.get("/documents/statement/{patient}")
def document_statement(patient: str, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("reports.run"))):
    """Patient statement as a printable document — the same derived
    figures as GET /billing/statements/{patient} (Phase 7), rendered
    on the governed letterhead."""
    patient_ref = fhir._normalize_patient_ref(patient)
    patient_id = patient_ref.split("/", 1)[1]
    fhir._get_row("Patient", patient_id, session)  # 404 if unknown
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Claim",
                                   FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.created_at.asc(), FhirResource.id.asc())).all()
    lines = []
    totals = {"total": 0.0, "paid": 0.0, "adjustments": 0.0,
              "patient_responsibility": 0.0, "patient_paid": 0.0,
              "patient_due": 0.0}
    for row in rows:
        body = row.body or {}
        financials = revenue.claim_financials(session, row.resource_id, body)
        lines.append([row.resource_id, str(body.get("status") or ""),
                      _money_str(financials["total"]),
                      _money_str(financials["paid"]),
                      _money_str(financials["adjustments"]),
                      _money_str(financials["patient_due"])])
        for key in totals:
            totals[key] = _money(totals[key] + financials[key])
    meta = [("Patient", _patient_display(session, patient_ref)),
            ("Printed", _now().isoformat()),
            ("Printed by", user.username)]
    notes = [f"Total billed: {_money_str(totals['total'])}",
             f"Insurance paid: {_money_str(totals['paid'])}",
             f"Patient responsibility: "
             f"{_money_str(totals['patient_responsibility'])}",
             f"Patient paid: {_money_str(totals['patient_paid'])}",
             f"<b>Amount you owe: {_money_str(totals['patient_due'])}</b>",
             revenue.PAYMENT_DISCLAIMER]
    _audit_print(session, user, "statement", patient_ref)
    return _pdf_response(_print_template(session), "Patient statement",
                         f"statement-{patient_id}.pdf", meta,
                         ["Claim", "Status", "Billed", "Paid",
                          "Adjustments", "You owe"], lines, notes)


@router.get("/documents/superbill/{encounter_id}")
def document_superbill(encounter_id: str, session: Session = Depends(get_db),
                       user: User = Depends(require_capability("reports.run"))):
    """Superbill for one encounter: the encounter's HIM-coded
    diagnoses and procedures (Phase 7 coding record) with fee-
    schedule prices — the document a small practice hands to its
    biller. Without a coding record there is nothing coded to bill:
    422, honestly, rather than an empty form."""
    encounter = fhir._get_row("Encounter", encounter_id, session)
    encounter_body = encounter.body or {}
    coding = billing.coding_record_for(session, encounter_id)
    if coding is None:
        raise HTTPException(
            status_code=422,
            detail=f"Encounter {encounter_id} has no HIM coding record; "
                   "a superbill is rendered from coded services")
    fee_index = billing._fee_index(session)
    lines = []
    total = 0.0
    for procedure in coding.procedures or []:
        if not isinstance(procedure, dict):
            continue
        price = billing.price_for(fee_index, procedure.get("system"),
                                  procedure.get("code"))
        if price is not None:
            total = _money(total + price)
        lines.append([str(procedure.get("code") or ""),
                      str(procedure.get("display") or ""),
                      _money_str(price) if price is not None
                      else "unpriced"])
    diagnoses = [f"{d.get('code', '')} — {d.get('display', '')}"
                 for d in (coding.diagnoses or []) if isinstance(d, dict)]
    patient_ref = fhir._subject_ref(encounter_body)
    meta = [("Encounter", encounter_id),
            ("Patient", _patient_display(session, patient_ref)),
            ("Date", str((encounter_body.get("period") or {}).get("start")
                         or "")),
            ("Coded by", coding.coded_by),
            ("Printed", _now().isoformat()),
            ("Printed by", user.username)]
    notes = [f"Diagnoses: {'; '.join(diagnoses) if diagnoses else '—'}",
             f"<b>Total charges: {_money_str(total)}</b>",
             "Unpriced lines are flagged, never dropped (the Phase 4A "
             "charge-capture rule)."]
    _audit_print(session, user, "superbill", f"Encounter/{encounter_id}")
    return _pdf_response(_print_template(session), "Superbill",
                         f"superbill-{encounter_id}.pdf", meta,
                         ["CPT", "Procedure", "Fee"], lines, notes)


# ---------------------------------------------------------------------------
# Export centre — named, server-defined datasets only
# ---------------------------------------------------------------------------

def _patient_name(body: dict) -> str:
    name = (body.get("name") or [{}])[0]
    if name.get("text"):
        return str(name["text"])
    given = " ".join(name.get("given") or [])
    return f"{given} {name.get('family') or ''}".strip()


def _code_of(body: dict, field: str) -> str:
    concept = body.get(field) or {}
    coding = concept.get("coding") or [{}]
    first = coding[0] if coding else {}
    return str(first.get("code") or concept.get("text") or "")


def _ds_patients(session: Session) -> list[dict]:
    return [{"id": r.resource_id, "name": _patient_name(r.body or {}),
             "gender": (r.body or {}).get("gender") or "",
             "birth_date": (r.body or {}).get("birthDate") or ""}
            for r in session.scalars(select(FhirResource).where(
                FhirResource.resource_type == "Patient").order_by(
                FhirResource.resource_id.asc())).all()]


def _ds_appointments(session: Session) -> list[dict]:
    rows = []
    for r in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Appointment").order_by(
            FhirResource.created_at.asc())).all():
        body = r.body or {}
        participants = body.get("participant") or []
        actor = ((participants[0].get("actor") or {}).get("reference")
                 if participants else "")
        service = (body.get("serviceType") or [{}])[0]
        rows.append({"id": r.resource_id, "patient": actor or "",
                     "status": body.get("status") or "",
                     "start": body.get("start") or "",
                     "end": body.get("end") or "",
                     "type": service.get("text") or ""})
    return rows


def _ds_orders(session: Session) -> list[dict]:
    rows = []
    for resource_type, field in (("MedicationRequest", "medicationCodeableConcept"),
                                 ("ServiceRequest", "code")):
        for r in session.scalars(select(FhirResource).where(
                FhirResource.resource_type == resource_type).order_by(
                FhirResource.created_at.asc())).all():
            body = r.body or {}
            rows.append({"id": r.resource_id, "type": resource_type,
                         "patient": r.patient_ref or "",
                         "status": body.get("status") or "",
                         "code": _code_of(body, field),
                         "authored_on": body.get("authoredOn") or ""})
    return rows


def _ds_lab_results(session: Session) -> list[dict]:
    rows = []
    for r in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "DiagnosticReport").order_by(
            FhirResource.created_at.asc())).all():
        body = r.body or {}
        rows.append({"id": r.resource_id, "patient": r.patient_ref or "",
                     "code": _code_of(body, "code"),
                     "status": body.get("status") or "",
                     "issued": body.get("issued")
                     or body.get("effectiveDateTime") or ""})
    return rows


def _ds_billing_lines(session: Session) -> list[dict]:
    rows = []
    for r in session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Claim").order_by(
            FhirResource.created_at.asc())).all():
        body = r.body or {}
        patient_ref = (body.get("patient") or {}).get("reference") or ""
        for item in body.get("item") or []:
            coding = _coding_of(item)
            rows.append({"claim_id": r.resource_id, "patient": patient_ref,
                         "sequence": item.get("sequence") or "",
                         "code": coding.get("code") or "",
                         "display": coding.get("display") or "",
                         "amount": (item.get("net") or {}).get("value")})
    return rows


EXPORT_DATASETS = {
    "patients": ("Patients", "All Patient resources (id, name, gender, "
                 "birth date).", _ds_patients),
    "appointments": ("Appointments", "All Appointment resources with "
                     "their patient participant and status.", _ds_appointments),
    "orders": ("Orders", "MedicationRequest + ServiceRequest orders with "
               "status and code.", _ds_orders),
    "lab-results": ("Lab results", "DiagnosticReport resources with "
                    "status and issue time.", _ds_lab_results),
    "billing-lines": ("Billing lines", "Claim charge lines flattened "
                      "(one row per line).", _ds_billing_lines),
}


@router.get("/exports/datasets")
def export_datasets(session: Session = Depends(get_db),
                    user: User = Depends(require_capability("data.export"))):
    return {"items": [{"key": key, "title": title, "description": description}
                      for key, (title, description, _fn)
                      in EXPORT_DATASETS.items()],
            "total": len(EXPORT_DATASETS),
            "reason_required_above_rows": _export_threshold(session)}


@router.post("/exports")
async def export_dataset(request: Request, session: Session = Depends(get_db),
                         user: User = Depends(require_capability("data.export"))):
    body = await _json_body(request)
    dataset = body.get("dataset")
    if dataset not in EXPORT_DATASETS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown dataset '{dataset}'; defined datasets: "
                   f"{sorted(EXPORT_DATASETS)} (exports are named, "
                   "server-defined queries — there is no ad-hoc export)")
    fmt = body.get("format") or "csv"
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=422,
                            detail="'format' must be 'csv' or 'json'")
    _title, _desc, fn = EXPORT_DATASETS[dataset]
    rows = fn(session)
    threshold = _export_threshold(session)
    reason = body.get("reason")
    if len(rows) > threshold and not (isinstance(reason, str)
                                       and reason.strip()):
        raise HTTPException(
            status_code=422,
            detail=f"Export of '{dataset}' has {len(rows)} rows, above "
                   f"the published threshold of {threshold}: a 'reason' "
                   "is required and is recorded in the audit")
    append_audit(session, actor=user.username, action="data.export",
                 entity=f"dataset:{dataset}",
                 payload={"dataset": dataset, "format": fmt,
                          "rows": len(rows),
                          "reason": reason if isinstance(reason, str)
                          else None})
    session.commit()
    headers = {"X-Export-Rows": str(len(rows)),
               "Content-Disposition":
               f'attachment; filename="{dataset}.{fmt}"'}
    if fmt == "json":
        return JSONResponse({"dataset": dataset, "total": len(rows),
                             "rows": rows}, headers=headers)
    columns = [{"field": k, "title": k} for k in
               (list(rows[0].keys()) if rows else ["id"])]
    return PlainTextResponse(_csv_text(columns, rows), media_type="text/csv",
                             headers=headers)


# ---------------------------------------------------------------------------
# Import centre — mapping profiles, validation pass, all-or-nothing
# ---------------------------------------------------------------------------

def _apply_transform(value: str, column: dict) -> tuple[str, str | None]:
    """Apply one governed transform. Returns (value, error|None)."""
    transform = column.get("transform")
    if not transform:
        return value, None
    if transform == "trim":
        return value.strip(), None
    if transform == "upper":
        return value.upper(), None
    if transform == "lower":
        return value.lower(), None
    if transform == "title":
        return value.title(), None
    if transform in ("date-mdY", "date-dMY"):
        pattern = "%m/%d/%Y" if transform == "date-mdY" else "%d/%m/%Y"
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat(), None
        except ValueError:
            return value, f"not a date in {pattern.replace('%', '')} format"
    if transform == "code-map":
        code_map = column.get("codeMap") or {}
        if value in code_map:
            return code_map[value], None
        if value in code_map.values():
            return value, None
        return value, f"'{value}' is not in the profile's code map"
    return value, f"unknown transform '{transform}'"


def _parse_csv(csv_text: str) -> tuple[list[str], list[dict]]:
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise HTTPException(status_code=422,
                            detail="'csv' must be non-empty CSV text")
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise HTTPException(status_code=422,
                            detail="CSV has no header row")
    headers = [h for h in reader.fieldnames if h is not None]
    rows = []
    for raw in reader:
        rows.append({(k.strip() if isinstance(k, str) else k):
                     (v if isinstance(v, str) else "")
                     for k, v in raw.items() if k is not None})
    return headers, rows


def _map_rows(profile: dict, headers: list[str],
              raw_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply the profile's column mappings + transforms. Returns
    (mapped rows, errors) where errors are {row, field, reason} with
    row = the CSV line number (header is line 1)."""
    errors: list[dict] = []
    columns = profile.get("columns") or []
    for column in columns:
        if column.get("source") not in headers:
            errors.append({"row": 1, "field": column.get("source"),
                           "reason": f"source column '{column.get('source')}' "
                                     "is not in the CSV header"})
    if errors:
        return [], errors
    mapped: list[dict] = []
    for index, raw in enumerate(raw_rows):
        line = index + 2
        out: dict = {}
        for column in columns:
            value = raw.get(column["source"], "")
            value, error = _apply_transform(value or "", column)
            if error:
                errors.append({"row": line, "field": column["target"],
                               "reason": error})
            out[column["target"]] = value.strip() \
                if isinstance(value, str) else value
        out["_line"] = line
        mapped.append(out)
    return mapped, errors


_GENDERS = ("male", "female", "other", "unknown")


def _validate_patients(session: Session, mapped: list[dict],
                       errors: list[dict]) -> list[dict]:
    seen_ids: set[str] = set()
    ready = []
    for row in mapped:
        line = row["_line"]
        if not row.get("family_name"):
            errors.append({"row": line, "field": "family_name",
                           "reason": "family name is required"})
        gender = row.get("gender")
        if gender and gender not in _GENDERS:
            errors.append({"row": line, "field": "gender",
                           "reason": f"gender must be one of {list(_GENDERS)}"})
        birth_date = row.get("birth_date")
        if birth_date and _days_to_expiry(birth_date) is None:
            errors.append({"row": line, "field": "birth_date",
                           "reason": "birth date must be an ISO date"})
        patient_id = row.get("id") or f"pat-imp-{uuid.uuid4().hex[:10]}"
        if patient_id in seen_ids:
            errors.append({"row": line, "field": "id",
                           "reason": f"duplicate patient id '{patient_id}' "
                                     "in this file"})
        elif session.scalars(select(FhirResource).where(
                FhirResource.resource_type == "Patient",
                FhirResource.resource_id == patient_id)).first() is not None:
            errors.append({"row": line, "field": "id",
                           "reason": f"patient '{patient_id}' already exists"})
        seen_ids.add(patient_id)
        name: dict = {"family": row.get("family_name") or ""}
        if row.get("given_name"):
            name["given"] = [row["given_name"]]
        name["text"] = f"{row.get('given_name', '')} {row.get('family_name', '')}".strip()
        body = {"resourceType": "Patient", "id": patient_id, "name": [name]}
        if gender:
            body["gender"] = gender
        if birth_date:
            body["birthDate"] = birth_date
        ready.append(body)
    return ready


def _validate_appointments(session: Session, mapped: list[dict],
                           errors: list[dict]) -> list[dict]:
    ready = []
    for row in mapped:
        line = row["_line"]
        patient_id = row.get("patient_id")
        if not patient_id:
            errors.append({"row": line, "field": "patient_id",
                           "reason": "patient id is required"})
        elif session.scalars(select(FhirResource).where(
                FhirResource.resource_type == "Patient",
                FhirResource.resource_id == patient_id)).first() is None:
            errors.append({"row": line, "field": "patient_id",
                           "reason": f"patient '{patient_id}' does not exist"})
        start = row.get("start")
        if not start:
            errors.append({"row": line, "field": "start",
                           "reason": "start is required"})
        else:
            try:
                datetime.fromisoformat(start)
            except ValueError:
                errors.append({"row": line, "field": "start",
                               "reason": "start must be an ISO datetime"})
        end = row.get("end")
        if end:
            try:
                datetime.fromisoformat(end)
            except ValueError:
                errors.append({"row": line, "field": "end",
                               "reason": "end must be an ISO datetime"})
        status = row.get("status") or "booked"
        if status not in DEFAULT_ADT_TRANSITIONS:
            errors.append({"row": line, "field": "status",
                           "reason": f"status must be one of "
                                     f"{sorted(DEFAULT_ADT_TRANSITIONS)}"})
        body = {"resourceType": "Appointment",
                "id": row.get("id") or f"appt-imp-{uuid.uuid4().hex[:10]}",
                "status": status, "start": start,
                "participant": [{"actor": {"reference": f"Patient/{patient_id}"},
                                 "status": "accepted"}]}
        if end:
            body["end"] = end
        if row.get("type"):
            body["serviceType"] = [{"text": row["type"]}]
        ready.append(body)
    return ready


def _validate_pharmacy_lots(session: Session, mapped: list[dict],
                            errors: list[dict]) -> list[dict]:
    ops = pharmacy_ops._ops(session)
    locations = set(pharmacy_ops._location_codes(ops))
    merged: dict[tuple[str, str, str], dict] = {}
    order: list[dict] = []
    for row in mapped:
        line = row["_line"]
        code = row.get("medication_code")
        entry = pharmacy._formulary_entry(session, code) if code else None
        if not code:
            errors.append({"row": line, "field": "medication_code",
                           "reason": "medication code is required"})
        elif entry is None:
            errors.append({"row": line, "field": "medication_code",
                           "reason": f"'{code}' is not in the published "
                                     "formulary"})
        location = row.get("location")
        if not location:
            errors.append({"row": line, "field": "location",
                           "reason": "location is required"})
        elif location not in locations:
            errors.append({"row": line, "field": "location",
                           "reason": f"'{location}' is not a pharmacy stock "
                                     f"location in config {sorted(locations)}"})
        lot_number = row.get("lot_number")
        if not lot_number:
            errors.append({"row": line, "field": "lot_number",
                           "reason": "lot number is required"})
        expiry = row.get("expiry_date")
        if not expiry:
            errors.append({"row": line, "field": "expiry_date",
                           "reason": "expiry date is required"})
        elif _days_to_expiry(expiry) is None:
            errors.append({"row": line, "field": "expiry_date",
                           "reason": "expiry date must be an ISO date"})
        quantity_raw = row.get("quantity")
        quantity = None
        try:
            quantity = int(str(quantity_raw))
            if quantity <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append({"row": line, "field": "quantity",
                           "reason": "quantity must be a positive integer"})
        if not code or not location or not lot_number or not expiry \
                or quantity is None:
            continue
        existing = session.scalars(select(PharmacyLot).where(
            PharmacyLot.medication_code == code,
            PharmacyLot.location == location,
            PharmacyLot.lot_number == lot_number)).first()
        if existing is not None and existing.expiry_date != expiry:
            errors.append({"row": line, "field": "expiry_date",
                           "reason": f"lot '{lot_number}' already exists at "
                                     f"{location} with expiry "
                                     f"{existing.expiry_date}, not {expiry}"})
            continue
        key = (code, location, lot_number)
        if key in merged:
            if merged[key]["expiry_date"] != expiry:
                errors.append({"row": line, "field": "expiry_date",
                               "reason": "same lot appears twice with "
                                         "different expiry dates"})
                continue
            merged[key]["quantity"] += quantity
        else:
            record = {"medication_code": code, "location": location,
                      "lot_number": lot_number, "expiry_date": expiry,
                      "quantity": quantity, "_existing": existing}
            merged[key] = record
            order.append(record)
    return order


_IMPORT_VALIDATORS = {
    "patients": _validate_patients,
    "appointments": _validate_appointments,
    "pharmacy-lots": _validate_pharmacy_lots,
}


def _run_import_validation(session: Session, profile_key: str,
                           csv_text: str) -> dict:
    cfg = published_config(session, "mapping", profile_key)
    if cfg is None:
        raise HTTPException(
            status_code=404,
            detail=f"No published mapping profile '{profile_key}'")
    profile = cfg.payload or {}
    entity_type = profile.get("entityType")
    headers, raw_rows = _parse_csv(csv_text)
    mapped, errors = _map_rows(profile, headers, raw_rows)
    # Entity-level checks ALWAYS run, even when mapping already found
    # problems: the validation report must be complete (every bad row,
    # field and reason in one pass), not stop at the first error class.
    # The validators tolerate partially-mapped rows; commit refuses on
    # ANY error, so `ready` is only consumed when errors is empty.
    ready = _IMPORT_VALIDATORS[entity_type](session, mapped, errors)
    return {"profile": profile_key, "entity_type": entity_type,
            "rows_total": len(raw_rows), "errors": errors,
            "valid": not errors, "ready": ready, "mapped": mapped}


@router.get("/imports/profiles")
def import_profiles(session: Session = Depends(get_db),
                    user: User = Depends(require_capability("data.import"))):
    items = []
    for cfg in published_configs(session, "mapping"):
        payload = cfg.payload or {}
        items.append({"key": cfg.key, "entity_type": payload.get("entityType"),
                      "description": payload.get("description"),
                      "columns": payload.get("columns") or [],
                      "version": cfg.version})
    return {"items": items, "total": len(items)}


@router.post("/imports/validate")
async def import_validate(request: Request, session: Session = Depends(get_db),
                          user: User = Depends(require_capability("data.import"))):
    """The validation pass: full error report, NOTHING written. The
    web import centre shows this before offering commit."""
    body = await _json_body(request)
    result = _run_import_validation(session, body.get("profile") or "",
                                    body.get("csv"))
    preview = [{k: v for k, v in row.items() if k != "_line"}
               for row in result["mapped"][:5]]
    return {"profile": result["profile"], "entity_type": result["entity_type"],
            "rows_total": result["rows_total"], "valid": result["valid"],
            "errors": result["errors"], "preview": preview}


@router.post("/imports/commit", status_code=201)
async def import_commit(request: Request, session: Session = Depends(get_db),
                        user: User = Depends(require_capability("data.import"))):
    """Commit = validate again, then create everything in ONE
    transaction. Any error -> 422 with the full report and NOTHING
    imported (the 835 all-or-nothing rule)."""
    body = await _json_body(request)
    profile_key = body.get("profile") or ""
    result = _run_import_validation(session, profile_key, body.get("csv"))
    if result["errors"]:
        raise HTTPException(
            status_code=422,
            detail={"message": "Import refused: validation failed; "
                               "nothing was imported",
                    "errors": result["errors"]})
    entity_type = result["entity_type"]
    ready = result["ready"]
    try:
        if entity_type in ("patients", "appointments"):
            resource_type = "Patient" if entity_type == "patients" \
                else "Appointment"
            for resource_body in ready:
                row = FhirResource(resource_type=resource_type,
                                   resource_id=resource_body["id"],
                                   version=1, body=resource_body)
                fhir._apply_projections(row, resource_type, resource_body,
                                        resource_body["id"])
                session.add(row)
            session.flush()
        elif entity_type == "pharmacy-lots":
            for record in ready:
                lot = record.pop("_existing")
                if lot is None:
                    lot = PharmacyLot(
                        medication_code=record["medication_code"],
                        location=record["location"],
                        lot_number=record["lot_number"],
                        expiry_date=record["expiry_date"], quantity=0)
                    session.add(lot)
                    session.flush()
                lot.quantity += record["quantity"]
                entry = pharmacy._formulary_entry(
                    session, record["medication_code"])
                if entry is not None and entry.get("stockTracked"):
                    pharmacy._stock_row(session, record["medication_code"],
                                        entry).quantity += record["quantity"]
                session.add(DscsaLedgerEntry(
                    entry_id=f"dscsa-{uuid.uuid4()}", event="receiving",
                    medication_code=record["medication_code"],
                    lot_number=record["lot_number"], serial=None,
                    expiry_date=record["expiry_date"],
                    quantity=record["quantity"],
                    reference=f"import:{profile_key}",
                    detail={"source": "import-centre",
                            "location": record["location"]},
                    actor=user.username))
            session.flush()
    except Exception:
        session.rollback()
        raise HTTPException(
            status_code=422,
            detail="Import failed during commit; the transaction was "
                   "rolled back and nothing was imported")
    append_audit(session, actor=user.username, action="data.import",
                 entity=f"import:{profile_key}",
                 payload={"profile": profile_key, "entity_type": entity_type,
                          "rows": len(ready)})
    session.commit()
    return {"imported": len(ready), "entity_type": entity_type,
            "profile": profile_key}


# ---------------------------------------------------------------------------
# Seeds: print template, export policy, report definitions, mapping
# profiles (all published v1, through governance)
# ---------------------------------------------------------------------------

def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    if list_config_versions(session, kind, key):
        return
    from .models import ConfigStatus, StudioConfig
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


def default_report_payloads() -> dict[str, dict]:
    return {
        "census-by-unit": {
            "title": "Census by unit",
            "description": "Bed state and occupancy per unit, live.",
            "source": "census-by-unit", "parameters": [],
            "formats": ["pdf", "csv"]},
        "claims-summary": {
            "title": "Claims summary by status",
            "description": "Claim counts and money rollups per lifecycle "
                           "status, from posted remittance records.",
            "source": "claims-summary", "parameters": [],
            "formats": ["pdf", "csv"]},
        "credential-expiry": {
            "title": "Credential expiry report",
            "description": "Staff credentials by expiry, optionally "
                           "limited to a window in days and/or scoped "
                           "to an org placement (region / location / "
                           "department / unit).",
            "source": "credential-expiry",
            "parameters": [{"name": "window_days", "type": "number",
                            "label": "Window (days)", "required": False},
                           {"name": "region_id", "type": "string",
                            "label": "Region", "required": False},
                           {"name": "location_id", "type": "string",
                            "label": "Location (facility)",
                            "required": False},
                           {"name": "department_id", "type": "string",
                            "label": "Department", "required": False},
                           {"name": "unit", "type": "string",
                            "label": "Unit", "required": False}],
            "formats": ["pdf", "csv"]},
        "safety-events": {
            "title": "Safety events report",
            "description": "Safety event volumes (reporter identities "
                           "are never part of this report).",
            "source": "safety-events",
            "parameters": [{"name": "status", "type": "choice",
                            "label": "Status", "required": False,
                            "choices": ["all", "open", "closed"],
                            "default": "all"}],
            "formats": ["pdf", "csv"]},
        "payments-summary": {
            "title": "Patient payments by method",
            "description": "Recorded patient payments grouped by method.",
            "source": "payments-summary", "parameters": [],
            "formats": ["pdf", "csv"]},
    }


def default_mapping_payloads() -> dict[str, dict]:
    return {
        "patient-intake": {
            "entityType": "patients",
            "description": "Front-desk patient intake spreadsheet.",
            "columns": [
                {"source": "MRN", "target": "id", "transform": "trim"},
                {"source": "Last Name", "target": "family_name",
                 "transform": "trim"},
                {"source": "First Name", "target": "given_name",
                 "transform": "trim"},
                {"source": "Sex", "target": "gender",
                 "transform": "code-map",
                 "codeMap": {"M": "male", "F": "female", "O": "other",
                             "U": "unknown"}},
                {"source": "DOB", "target": "birth_date",
                 "transform": "date-mdY"},
            ]},
        "appointment-load": {
            "entityType": "appointments",
            "description": "Bulk appointment load (e.g. a clinic "
                           "template week).",
            "columns": [
                {"source": "Patient ID", "target": "patient_id",
                 "transform": "trim"},
                {"source": "Start", "target": "start", "transform": "trim"},
                {"source": "Type", "target": "type", "transform": "trim"},
                {"source": "Status", "target": "status",
                 "transform": "code-map",
                 "codeMap": {"B": "booked", "C": "cancelled"}},
            ]},
        "pharmacy-lot-load": {
            "entityType": "pharmacy-lots",
            "description": "Opening pharmacy lot inventory load.",
            "columns": [
                {"source": "Medication", "target": "medication_code",
                 "transform": "trim"},
                {"source": "Location", "target": "location",
                 "transform": "trim"},
                {"source": "Lot", "target": "lot_number",
                 "transform": "trim"},
                {"source": "Expiry", "target": "expiry_date",
                 "transform": "date-mdY"},
                {"source": "Qty", "target": "quantity",
                 "transform": "trim"},
            ]},
    }


def ensure_reports_defaults(session: Session) -> None:
    _seed_published(session, "integration", PRINT_KEY, default_print_payload())
    _seed_published(session, "integration", EXPORTS_KEY,
                    default_exports_payload())
    for key, payload in default_report_payloads().items():
        _seed_published(session, "report", key, payload)
    for key, payload in default_mapping_payloads().items():
        _seed_published(session, "mapping", key, payload)
    session.commit()
