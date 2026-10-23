"""Regulatory compliance framework — Phase 10, Track J.

Three surfaces:

1. THE REQUIREMENTS REGISTER (ComplianceRequirement rows): every
   applicable requirement as data — body, citation, title, the
   operational control that satisfies it, where the control is
   implemented, and its EVIDENCE SOURCE. Seeded with the HIPAA
   baseline and the ONC/Cures entries; sites add their own through
   the API (writes are studio.manage-gated and hash-chain audited).

2. REGULATORY PACKS (Studio config kind=regulatory_pack): the
   governed parameter sets a regulation imposes on workflows —
   retention years, breach-notification clock, break-glass duration
   and justification window... Consumers read the PUBLISHED pack
   (app.privacy reads the HIPAA pack's break-glass parameters), so a
   regulation change is a config publish, never a code branch
   (Principle 6). Seeded as DRAFTS: hipaa-baseline, onc-cures.

3. EVIDENCE + TRACER: the evidence endpoint resolves a requirement's
   evidence source against the hash-chained audit (sources look like
   `audit:<action prefix>`, `config:<kind>/<key>`, or `file:<path>` —
   the last is reported as a document reference, never faked as data).
   The tracer walks one synthetic patient's records and checks named
   controls actually fired: chart access logged, break-glass openings
   reviewed, critical-result notifications completed, eligibility
   recorded where authorisations exist.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import (list_config_versions, published_config)
from .models import (AuditLog, BreakGlassAccess, ComplianceRequirement,
                     ConfigStatus, EligibilityCheck, FhirResource, PriorAuth,
                     StudioConfig, User)
from .security import get_db, require_capability

router = APIRouter(prefix="/compliance", tags=["compliance"])


# ---------------------------------------------------------------------------
# Requirements register
# ---------------------------------------------------------------------------

def _req_out(row: ComplianceRequirement) -> dict:
    return {"id": row.id, "body": row.body, "citation": row.citation,
            "title": row.title, "control": row.control,
            "implementation_ref": row.implementation_ref,
            "evidence_source": row.evidence_source,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


@router.get("/requirements")
def list_requirements(body: str | None = Query(default=None),
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("studio.read"))):
    stmt = select(ComplianceRequirement).order_by(
        ComplianceRequirement.body, ComplianceRequirement.id)
    if body:
        stmt = stmt.where(ComplianceRequirement.body == body)
    rows = session.scalars(stmt).all()
    return {"items": [_req_out(r) for r in rows], "total": len(rows),
            "bodies": sorted({r.body for r in rows})}


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return payload


def _require_fields(payload: dict) -> None:
    for field in ("body", "citation", "control"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise HTTPException(
                status_code=422,
                detail=f"Requirement requires a non-empty '{field}'")


@router.post("/requirements", status_code=201)
async def create_requirement(request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("studio.manage"))):
    payload = await _json_body(request)
    _require_fields(payload)
    row = ComplianceRequirement(
        body=payload["body"].strip(), citation=payload["citation"].strip(),
        title=str(payload.get("title") or ""), control=payload["control"].strip(),
        implementation_ref=str(payload.get("implementation_ref") or ""),
        evidence_source=str(payload.get("evidence_source") or ""),
        created_by=user.username)
    session.add(row)
    session.flush()
    append_audit(session, actor=user.username,
                 action="compliance.requirement.create",
                 entity=f"requirement:{row.body}/{row.citation}",
                 payload={"requirement_id": row.id, "body": row.body,
                          "citation": row.citation})
    session.commit()
    return _req_out(row)


@router.put("/requirements/{requirement_id}")
async def update_requirement(requirement_id: int, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("studio.manage"))):
    payload = await _json_body(request)
    _require_fields(payload)
    row = session.get(ComplianceRequirement, requirement_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Requirement {requirement_id} not found")
    row.body = payload["body"].strip()
    row.citation = payload["citation"].strip()
    row.title = str(payload.get("title") or "")
    row.control = payload["control"].strip()
    row.implementation_ref = str(payload.get("implementation_ref") or "")
    row.evidence_source = str(payload.get("evidence_source") or "")
    session.flush()
    append_audit(session, actor=user.username,
                 action="compliance.requirement.update",
                 entity=f"requirement:{row.body}/{row.citation}",
                 payload={"requirement_id": row.id})
    session.commit()
    return _req_out(row)


# ---------------------------------------------------------------------------
# Evidence: resolve a requirement's evidence source
# ---------------------------------------------------------------------------

@router.get("/requirements/{requirement_id}/evidence")
def requirement_evidence(requirement_id: int,
                         limit: int = Query(default=100, ge=1, le=500),
                         session: Session = Depends(get_db),
                         user: User = Depends(require_capability("studio.manage"))):
    """The audit-chain evidence for one requirement. The requirement's
    evidence_source names where proof lives; `audit:` sources are
    queries over the hash chain (the surveyor's answer is data, not a
    scramble), `config:` sources return the published config object,
    and `file:` sources are reported as document references."""
    row = session.get(ComplianceRequirement, requirement_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Requirement {requirement_id} not found")
    source = row.evidence_source or ""
    if source.startswith("audit:"):
        prefix = source[len("audit:"):]
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        if prefix:
            stmt = select(AuditLog).where(
                AuditLog.action.startswith(prefix)).order_by(
                AuditLog.id.desc()).limit(limit)
        entries = [{"id": e.id, "actor": e.actor, "action": e.action,
                    "entity": e.entity,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                    "entry_hash": e.entry_hash}
                   for e in session.scalars(stmt).all()]
        return {"requirement_id": row.id, "source": source,
                "kind": "audit", "entries": entries, "total": len(entries)}
    if source.startswith("config:"):
        ref = source[len("config:"):]
        kind, _, key = ref.partition("/")
        cfg = published_config(session, kind, key) if kind and key else None
        return {"requirement_id": row.id, "source": source, "kind": "config",
                "published": ({"kind": kind, "key": key,
                               "version": cfg.version} if cfg else None)}
    return {"requirement_id": row.id, "source": source, "kind": "reference",
            "note": ("Evidence source is a document reference (not "
                     "machine-resolvable data); it is reported as-is, "
                     "never faked into records.")}


# ---------------------------------------------------------------------------
# Tracer self-audit (v1)
# ---------------------------------------------------------------------------

def _tracer_checks(session: Session, patient_ref: str) -> list[dict]:
    checks: list[dict] = []

    # 1. Chart access logged: the hash chain carries entries naming
    #    this patient (entity or payload).
    hits = 0
    for entry in session.scalars(select(AuditLog)).all():
        if patient_ref in (entry.entity or "") \
                or patient_ref in json.dumps(entry.payload or {}, default=str):
            hits += 1
    checks.append({
        "control": "chart-access-logged",
        "status": "pass" if hits else "fail",
        "detail": (f"{hits} audit-chain entr{'y' if hits == 1 else 'ies'} "
                   f"reference {patient_ref}"
                   if hits else f"No audit-chain entries reference "
                                 f"{patient_ref} — access is not evidenced"),
    })

    # 2. Break-glass reviewed: every opening for this patient reviewed.
    openings = session.scalars(select(BreakGlassAccess).where(
        BreakGlassAccess.patient_ref == patient_ref)).all()
    if not openings:
        checks.append({"control": "break-glass-reviewed",
                       "status": "not-applicable",
                       "detail": "No break-glass openings for this patient"})
    else:
        unreviewed = [o for o in openings if o.reviewed_by is None]
        checks.append({
            "control": "break-glass-reviewed",
            "status": "fail" if unreviewed else "pass",
            "detail": (f"{len(unreviewed)} of {len(openings)} break-glass "
                       "openings are still unreviewed"
                       if unreviewed else
                       f"All {len(openings)} break-glass openings reviewed"),
        })

    # 3. Critical-result notifications completed for this patient.
    from . import lab as lab_module  # lazy: lab is a large module
    reports = session.scalars(select(FhirResource).where(
        FhirResource.resource_type == "DiagnosticReport",
        FhirResource.patient_ref == patient_ref)).all()
    critical_states = []
    for report in reports:
        order = lab_module._report_order(session, report)
        if order is None or (order.body or {}).get("status") != "released":
            continue
        if lab_module._report_is_critical(session, report):
            state = lab_module._notification_out(session, report, True)["state"]
            critical_states.append((report.resource_id, state))
    if not critical_states:
        checks.append({"control": "critical-results-completed",
                       "status": "not-applicable",
                       "detail": "No released critical results for this patient"})
    else:
        incomplete = [rid for rid, state in critical_states
                      if state != "acknowledged"]
        checks.append({
            "control": "critical-results-completed",
            "status": "fail" if incomplete else "pass",
            "detail": (f"Critical reports not yet acknowledged: "
                       f"{', '.join(incomplete)}"
                       if incomplete else
                       f"All {len(critical_states)} critical reports "
                       "acknowledged after read-back notification"),
        })

    # 4. Eligibility recorded where authorisations exist (CMS/Phase 7
    #    access discipline): a prior auth without any recorded
    #    eligibility check means coverage was never evidenced.
    auths = session.scalars(select(PriorAuth).where(
        PriorAuth.patient_ref == patient_ref)).all()
    elig = session.scalars(select(EligibilityCheck).where(
        EligibilityCheck.patient_ref == patient_ref)).all()
    if not auths and not elig:
        checks.append({"control": "eligibility-recorded",
                       "status": "not-applicable",
                       "detail": "No prior authorisations or eligibility "
                                 "checks for this patient"})
    elif auths and not elig:
        checks.append({"control": "eligibility-recorded",
                       "status": "fail",
                       "detail": (f"{len(auths)} prior authorisation(s) on "
                                  "file but no eligibility check was ever "
                                  "recorded")})
    else:
        checks.append({"control": "eligibility-recorded",
                       "status": "pass",
                       "detail": f"{len(elig)} eligibility check(s) recorded"})
    return checks


@router.post("/tracer")
async def tracer(request: Request, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("studio.manage"))):
    """Tracer self-audit v1: walk one patient's records and check the
    named controls actually fired. Synthetic patients only (the whole
    system is synthetic-data-only). The run itself is audited."""
    payload = await _json_body(request)
    patient_ref = payload.get("patient_ref") or payload.get("patient")
    if not isinstance(patient_ref, str) or not patient_ref.strip():
        raise HTTPException(status_code=422, detail="Tracer requires 'patient_ref'")
    from . import fhir
    patient_ref = fhir._normalize_patient_ref(patient_ref.strip())
    fhir._get_row("Patient", patient_ref.split("/", 1)[1], session)  # 404
    checks = _tracer_checks(session, patient_ref)
    append_audit(session, actor=user.username, action="compliance.tracer.run",
                 entity=patient_ref,
                 payload={"patient": patient_ref,
                          "results": {c["control"]: c["status"] for c in checks}})
    session.commit()
    return {"patient_ref": patient_ref, "checks": checks,
            "summary": {status: sum(1 for c in checks if c["status"] == status)
                        for status in ("pass", "fail", "not-applicable")}}


# ---------------------------------------------------------------------------
# Seeds: register entries (HIPAA + ONC/Cures) and draft regulatory packs
# ---------------------------------------------------------------------------

def default_requirement_seeds() -> list[dict]:
    return [
        # ---- HIPAA (45 CFR Part 164) ----
        {"body": "HIPAA", "citation": "45 CFR 164.312(b)",
         "title": "Audit controls",
         "control": ("Every clinical, configuration and governance write "
                     "appends a hash-chained audit entry (actor, action, "
                     "entity); the chain is verifiable end-to-end and "
                     "reviewable in the Studio audit view."),
         "implementation_ref": "backend/app/audit.py; GET /studio/audit",
         "evidence_source": "audit:"},
        {"body": "HIPAA", "citation": "45 CFR 164.312(a)(2)(ii)",
         "title": "Emergency access procedure (break-the-glass)",
         "control": ("Emergency chart access always works in the moment "
                     "for clinical roles, demands a reason, is time-boxed "
                     "by configuration, is flagged in the audit chain, "
                     "and is reviewed post-hoc at 100% (no sampling) "
                     "inside the configured justification window."),
         "implementation_ref": "backend/app/privacy.py; POST /privacy/break-glass",
         "evidence_source": "audit:breakglass.open"},
        {"body": "HIPAA", "citation": "45 CFR 164.502(b)",
         "title": "Minimum necessary",
         "control": ("Role/capability gates limit every surface to the "
                     "minimum necessary for the role; patient surfaces "
                     "are scoped to the caller's own record (another "
                     "patient's data is a 404, never disclosed)."),
         "implementation_ref": "backend/app/security.py (require_capability); "
                               "backend/app/configstore.py CAPABILITY_DEFAULTS",
         "evidence_source": "config:permissions/role-matrix"},
        {"body": "HIPAA", "citation": "45 CFR 164.404 / 164.308(a)(6)",
         "title": "Breach notification (60-day clock)",
         "control": ("Security incidents are identified through the "
                     "audit chain and privacy review queue; affected "
                     "individuals are notified without unreasonable "
                     "delay and no later than 60 days after discovery "
                     "(clock parameterised in the HIPAA pack: "
                     "breachNotificationDays)."),
         "implementation_ref": "backend/app/privacy.py review queue; "
                               "HIPAA regulatory pack parameter breachNotificationDays",
         "evidence_source": "audit:breakglass.review"},
        {"body": "HIPAA", "citation": "45 CFR 164.316(b)(2)(i)",
         "title": "Documentation retention (6 years)",
         "control": ("Required documentation (policies, audit records) "
                     "is retained 6 years from creation/last-effective "
                     "date; the period is a HIPAA pack parameter "
                     "(documentationRetentionYears), not a constant."),
         "implementation_ref": "HIPAA regulatory pack parameter "
                               "documentationRetentionYears",
         "evidence_source": "config:regulatory_pack/hipaa-baseline"},
        {"body": "HIPAA", "citation": "45 CFR 164.312(e)(1)",
         "title": "Transmission security",
         "control": ("PHI in transit is protected by TLS 1.2+ with "
                     "HSTS in any non-local deployment; the dev "
                     "plain-HTTP profile is local-only."),
         "implementation_ref": "web/nginx.tls.conf (TLS server template)",
         "evidence_source": "file:web/nginx.tls.conf"},
        # ---- ONC / Cures Act (45 CFR Part 170 / Part 171) ----
        {"body": "ONC/Cures", "citation": "45 CFR 170.315(g)(10)",
         "title": "Standardised API access (FHIR R4)",
         "control": ("Patient and population services are exposed over "
                     "a standard FHIR R4 API with tokenised access; "
                     "the API surface is the same one the web client "
                     "and SMART-style consumers use."),
         "implementation_ref": "backend/app/fhir.py (FHIR REST API)",
         "evidence_source": "audit:fhir.create"},
        {"body": "ONC/Cures", "citation": "45 CFR 170.315(b)(10)",
         "title": "Electronic health information (EHI) export",
         "control": ("System-level EHI export exists as auditable "
                     "bulk-export jobs (NDJSON), capability-gated to "
                     "admin/configurator, with every job and download "
                     "written to the audit chain."),
         "implementation_ref": "backend/app/interop.py bulk $export; "
                               "capability interop.export",
         "evidence_source": "audit:interop.export.create"},
        {"body": "ONC/Cures", "citation": "45 CFR Part 171",
         "title": "Information blocking — data sharing by default",
         "control": ("EHI is shared through the FHIR API, bulk export "
                     "and HL7 feeds unless a documented exception in "
                     "the ONC/Cures pack applies; refusals are policy "
                     "(pack data), never silent code paths."),
         "implementation_ref": "ONC/Cures regulatory pack (informationBlocking) "
                               "parameter section",
         "evidence_source": "audit:interop."},
        {"body": "ONC/Cures", "citation": "45 CFR 170.315(b)(11)",
         "title": "Decision support interventions — transparency",
         "control": ("AI/agent decision support is attributable: every "
                     "agent run records the agent, its published config "
                     "version, the model route and a hash of its inputs "
                     "in the audit chain, and its output is a draft a "
                     "human dispositions — agents never sign or act."),
         "implementation_ref": "backend/app/agents.py; backend/app/ai_governance.py "
                               "(per-agent disclosure endpoint "
                               "GET /ai-governance/agents/{key}/disclosure); Studio ADK tab",
         "evidence_source": "audit:agents.run; backend/app/ai_governance.py disclosure "
                            "endpoint (GET /ai-governance/agents/{key}/disclosure)"},
        # ---- Pharmacy (Phase 11): DEA / USP / DSCSA ----
        {"body": "DEA", "citation": "21 CFR 1304.04",
         "title": "Controlled substance records and inventory",
         "control": ("A perpetual controlled-substance inventory is "
                     "kept by lot and location; shift counts compare "
                     "expected vs counted with a second-person witness, "
                     "and a variance opens a discrepancy that must be "
                     "resolved with a recorded reason before the count "
                     "book for that medication+location balances."),
         "implementation_ref": "backend/app/pharmacy_ops.py (CS counts, "
                               "count book); DEA regulatory pack",
         "evidence_source": "audit:pharmacy.cs."},
        {"body": "DEA", "citation": "21 CFR Part 1317",
         "title": "Controlled substance disposal — witnessed waste",
         "control": ("Wasting a controlled substance (partial dose, "
                     "damaged unit) is a two-person witnessed act with "
                     "a recorded reason; the waste decrements the lot "
                     "and the perpetual inventory in the same "
                     "transaction and is written to the audit chain."),
         "implementation_ref": "backend/app/pharmacy_ops.py (CS waste)",
         "evidence_source": "audit:pharmacy.cs.waste"},
        {"body": "USP", "citation": "USP <797>",
         "title": "Sterile compounding — batch records, beyond-use dating",
         "control": ("Every compounded preparation has a batch record "
                     "capturing each ingredient's lot; the beyond-use "
                     "datetime is computed from the site's published "
                     "category configuration (budHours per storage "
                     "condition), never set by the preparer; release "
                     "requires a verifier who is not the preparer."),
         "implementation_ref": "backend/app/pharmacy_ops.py "
                               "(compounding); pharmacy-ops config "
                               "compounding categories",
         "evidence_source": "audit:pharmacy.compound."},
        {"body": "USP", "citation": "USP <800>",
         "title": "Hazardous drugs — handling in compounding and dispensing",
         "control": ("Hazardous-drug preparations run through the "
                     "same governed compounding batch workflow, with "
                     "the site's hazardous-drug list and handling "
                     "parameters carried in the USP regulatory pack and "
                     "the published formulary configuration, so HD "
                     "work is identifiable in the batch record and "
                     "audit chain."),
         "implementation_ref": "backend/app/pharmacy_ops.py "
                               "(compounding); USP regulatory pack",
         "evidence_source": "audit:pharmacy.compound."},
        {"body": "DSCSA", "citation": "FD&C Act §582 (21 USC 360eee)",
         "title": "Drug supply chain traceability",
         "control": ("Receiving captures the transaction data for "
                     "every line — lot, expiry, and unit serials when "
                     "the shipment carries them — into an append-only "
                     "traceability ledger; dispensing allocates FEFO "
                     "and records the lot and serials consumed, so "
                     "any unit can be traced from receipt to patient."),
         "implementation_ref": "backend/app/pharmacy_ops.py "
                               "(procurement/traceability); "
                               "backend/app/pharmacy.py (dispense "
                               "allocation); DSCSA regulatory pack",
         "evidence_source": "audit:pharmacy.procurement."},
        # ---- Research governance (Phase 16, WS-F2): GCP / GLP /
        # Part 11. The packs are seeded as labelled DRAFTS by
        # app.research_ops / app.esignature; legal attestation is out
        # of scope — these entries name the operating controls.
        {"body": "ICH-GCP", "citation": "ICH E6(R2) 8.1",
         "title": "Essential documents retained per protocol",
         "control": ("The gcp-ich-e6 regulatory pack's essentialDocuments "
                     "parameter names the essential-document set; study "
                     "records in app.research_ops carry the protocol, "
                     "approvals and determinations those documents govern."),
         "implementation_ref": "backend/app/research_ops.py; regulatory pack gcp-ich-e6",
         "evidence_source": "config:regulatory_pack/gcp-ich-e6"},
        {"body": "ICH-GCP", "citation": "ICH E6(R2) 5.18",
         "title": "Risk-based monitoring of clinical investigations",
         "control": ("Monitoring cadence is governed data: the "
                     "gcp-ich-e6 pack's monitoringVisitCadenceDays "
                     "parameter sets the visit cadence studies are "
                     "monitored against."),
         "implementation_ref": "backend/app/research_ops.py; regulatory pack gcp-ich-e6",
         "evidence_source": "config:regulatory_pack/gcp-ich-e6"},
        {"body": "FDA-GLP", "citation": "21 CFR 58.195",
         "title": "Retention of raw data (GLP)",
         "control": ("Raw-data retention is governed data: the "
                     "glp-21cfr58-oecd pack's rawDataRetentionYears "
                     "parameter sets the retention period for nonclinical "
                     "laboratory study records."),
         "implementation_ref": "backend/app/research_ops.py; regulatory pack glp-21cfr58-oecd",
         "evidence_source": "config:regulatory_pack/glp-21cfr58-oecd"},
        {"body": "FDA-GLP", "citation": "21 CFR 58.29",
         "title": "Quality assurance unit audits (GLP)",
         "control": ("QA audit cadence is governed data: the "
                     "glp-21cfr58-oecd pack's qaAuditCadenceDays "
                     "parameter sets how often the QA unit audits "
                     "facilities and study phases."),
         "implementation_ref": "backend/app/research_ops.py; regulatory pack glp-21cfr58-oecd",
         "evidence_source": "config:regulatory_pack/glp-21cfr58-oecd"},
        {"body": "FDA", "citation": "21 CFR 11.50",
         "title": "Signature manifestations (Part 11)",
         "control": ("Every electronic signature stores signer, meaning "
                     "and timestamp plus a binding hash over the signed "
                     "record's snapshot; the manifestation is served at "
                     "GET /esign/manifest so a signed record always "
                     "shows who signed, why, and when."),
         "implementation_ref": "backend/app/esignature.py",
         "evidence_source": "audit:esign.sign"},
        # ---- Phase 17 (WS-SEC): KMS, privacy monitoring, log
        # redaction. CMP-30/31/32 in the requirements register.
        {"body": "HIPAA", "citation": "45 CFR 164.312(a)(2)(iv) / CMP-30",
         "title": "Encryption key management and rotation",
         "control": ("Envelope-encryption KEKs are held behind a key-"
                     "provider interface (deployment env/key-file, or "
                     "the local software-KMS — a software key store, "
                     "explicitly NOT an HSM); rotation runs on the "
                     "published Studio schedule, DEKs are re-wrapped "
                     "onto the new KEK, and the retired key's material "
                     "is destroyed so it can no longer unwrap."),
         "implementation_ref": "backend/app/keymgmt.py; "
                               "GET /security/kms/status",
         "evidence_source": "audit:kms."},
        {"body": "HIPAA", "citation": "45 CFR 164.312(b) / CMP-31",
         "title": "Access-pattern (snooping) monitoring",
         "control": ("The hash-chained audit is analysed under "
                     "governed, published detection rules (VIP / "
                     "coworker / family access without a treatment "
                     "relationship, volume anomalies, after-hours "
                     "patterns); every hit opens a privacy-workqueue "
                     "case for officer review alongside the break-"
                     "glass queue — monitoring opens cases, it never "
                     "accuses."),
         "implementation_ref": "backend/app/privacy_monitor.py; "
                               "POST /privacy-monitor/run",
         "evidence_source": "audit:privacy.monitor."},
        {"body": "HIPAA", "citation": "45 CFR 164.514(d) / CMP-32",
         "title": "PHI redaction at the logging boundary",
         "control": ("Structured logs in every service (backend, "
                     "ADK, Go, Rust) pass a redaction boundary whose "
                     "rules are governed Studio config over a "
                     "versioned built-in rule set; a synthetic-day "
                     "log scan proves zero unredacted PHI patterns "
                     "survive in emitted logs."),
         "implementation_ref": "backend/app/logredact.py; "
                               "GET /security/log-redaction/status",
         "evidence_source": "config:integration/log-redaction"},
        {"body": "FDA", "citation": "21 CFR 11.70",
         "title": "Signature/record linking (Part 11)",
         "control": ("binding_hash = sha256(snapshotHash + signer + "
                     "meaning + timestamp) links each signature to the "
                     "exact record state it attests; signing a record "
                     "that changed since the signer's snapshot is "
                     "refused (409), so a signature can never silently "
                     "attach to different content."),
         "implementation_ref": "backend/app/esignature.py (sign_record)",
         "evidence_source": "audit:esign.sign"},
    ]


def default_regulatory_pack_payloads() -> dict[str, dict]:
    return {
        "hipaa-baseline": {
            "body": "HIPAA",
            "jurisdiction": "US",
            "parameters": {
                "breachNotificationDays": 60,
                "documentationRetentionYears": 6,
                "medicalRecordRetentionYears": 6,
                "breakGlassDurationMinutes": 60,
                "justificationWindowHours": 48,
                "auditReviewCadenceDays": 30,
                "minimumNecessary": True,
            },
            "requirements": ["45 CFR 164.312(b)", "45 CFR 164.312(a)(2)(ii)",
                             "45 CFR 164.502(b)", "45 CFR 164.404",
                             "45 CFR 164.316(b)(2)(i)", "45 CFR 164.312(e)(1)"],
            "notes": ("US HIPAA baseline. Parameters are consumed by "
                      "Track J/K code (app.privacy reads the break-glass "
                      "duration + justification window from the "
                      "PUBLISHED version of this pack). Seeded as a "
                      "draft: review, adjust to your organisation's "
                      "policies, then publish."),
        },
        "onc-cures": {
            "body": "ONC/Cures",
            "jurisdiction": "US",
            "parameters": {
                "fhirVersion": "R4",
                "usCoreProfiles": True,
                "informationBlocking": "share-by-default",
                "ehiExportEnabled": True,
                "apiAccess": "tokenised",
            },
            "requirements": ["45 CFR 170.315(g)(10)", "45 CFR 170.315(b)(10)",
                             "45 CFR Part 171", "45 CFR 170.315(b)(11)"],
            "notes": ("ONC Health IT Certification / Cures Act baseline. "
                      "Seeded as a draft alongside the HIPAA pack."),
        },
        # ---- Pharmacy packs (Phase 11, Track J): seeded as drafts,
        # exactly like the HIPAA/ONC packs — a site reviews and
        # publishes them through governance before they bind.
        "dea-controlled-substances": {
            "body": "DEA",
            "jurisdiction": "US",
            "parameters": {
                "csCountFrequency": "per-shift",
                "twoPersonCounts": True,
                "twoPersonWaste": True,
                "discrepancyResolutionRequired": True,
                "schedules": "II,III,IV,V",
            },
            "requirements": ["21 CFR 1304.04", "21 CFR Part 1317"],
            "notes": ("DEA / state-board controlled-substances baseline. "
                      "The operational controls live in app.pharmacy_ops "
                      "(counts, witnessed waste, discrepancy resolution "
                      "against the perpetual lot book); state boards may "
                      "tighten count frequency — that is this pack's "
                      "parameters, not code. Seeded as a draft (Phase 11)."),
        },
        "usp-compounding": {
            "body": "USP",
            "jurisdiction": "US",
            "parameters": {
                "budSource": "terminology/pharmacy-ops compounding categories",
                "twoPersonVerification": True,
                "hazardousDrugsChapter": "<800>",
                "ingredientLotCapture": True,
            },
            "requirements": ["USP <797>", "USP <800>"],
            "notes": ("USP <797>/<800> sterile-compounding baseline. "
                      "Beyond-use hours per category and storage condition "
                      "are the pharmacy-ops config's compounding section — "
                      "the site publishes the values from the chapter "
                      "table it has adopted. Seeded as a draft (Phase 11)."),
        },
        "dscsa-traceability": {
            "body": "DSCSA",
            "jurisdiction": "US",
            "parameters": {
                "captureLotSerialExpiryAtReceiving": True,
                "serialConsumptionAtDispense": True,
                "fefoAllocation": True,
            },
            "requirements": ["FD&C Act §582 (21 USC 360eee)"],
            "notes": ("Drug Supply Chain Security Act traceability. "
                      "Receiving captures lot/serial/expiry into the "
                      "ledger (app.pharmacy_ops); dispensing records the "
                      "lot and serials consumed (app.pharmacy). Seeded "
                      "as a draft (Phase 11)."),
        },
    }


def _seed_pack_draft(session: Session, key: str, payload: dict) -> None:
    if list_config_versions(session, "regulatory_pack", key):
        return
    row = StudioConfig(kind="regulatory_pack", key=key, version=1,
                       status=ConfigStatus.draft, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:regulatory_pack/{key}@v1",
                 payload={"kind": "regulatory_pack", "key": key, "version": 1,
                          "seeded_default": True})


def ensure_compliance_defaults(session: Session) -> None:
    if not session.scalars(select(ComplianceRequirement)).first():
        for seed in default_requirement_seeds():
            session.add(ComplianceRequirement(created_by="system", **seed))
        session.flush()
        append_audit(session, actor="system",
                     action="compliance.register.seed", entity="register",
                     payload={"entries": len(default_requirement_seeds()),
                              "bodies": sorted({s["body"] for s in
                                                default_requirement_seeds()})})
    for key, payload in default_regulatory_pack_payloads().items():
        _seed_pack_draft(session, key, payload)
    session.commit()
