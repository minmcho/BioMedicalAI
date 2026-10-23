import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import access, access_services, accounts, agent_validation, agent_workflows, agents, ai_governance, ai_providers, analytics, barcodes, billing, boards, breach_ops, care_coordination, care_management, compliance, continuum, dbplatform, devices, directories, doc_intake, documentation, empi, engagement, esignature, federation, fhir, gateway, genomics, graphql_lane, home_cards, imaging_assist, indicators, infection_prevention, innovation, inpatient, interop, inventory, jurisdictions, keymgmt, lab, lab_analyser, lab_autoverify, lab_benches, lab_compendium, lab_depth, lab_micro, lab_molecular, lab_plates, lab_preanalytical, lab_qc, lab_reagents, lab_poct_qms, lab_specimens, lab_tat, lab_transfusion, listmgmt, logredact, maintenance, mcp_a2a, observability, onboarding, orders, org_hierarchy, outbox, pack_gate, people, pharmacy, pharmacy_ops, pipelines, platform_depth, pophealth, print_queue, privacy, privacy_monitor, protocols, qrda_export, quality, radiology, rag, realtime, registration, report_engine, reports, research_ops, results_inbox, revenue, rfid, rules, session_security, studio, studio_config, studio_ops, support_ops, support_services, surgery_depth, teams, tiering  # noqa: F401
from .configstore import (ensure_inventory_defaults,
                          ensure_job_profiles_default, published_config)
from .db import Base, make_engine, make_session_factory
from .security import seed_users


def create_app(database_url: str | None = None) -> FastAPI:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    # Organisation & Registration Placement pass: create_all never
    # alters an existing table, so the placement columns are added
    # additively (inspector-guarded) for databases created before
    # this pass. No-op on fresh databases.
    org_hierarchy.ensure_org_columns(engine)
    # Gap Wave A (PHARM-B1): the pharmacist-verification columns ride
    # the same additive pattern for databases created before Wave A.
    pharmacy_ops.ensure_pharmacy_verification_columns(engine)
    # Inventory Depth pass: the depth columns (unit ladder, kits,
    # containers' lot fields, lot stages + verification, PAR levels)
    # ride the same additive pattern for databases created before
    # the pass. No-op on fresh databases.
    inventory.ensure_inventory_columns(engine)
    # Lab Depth pass (LAB-D8): the QC keying columns on lab_qc_runs
    # (instrument / reagent lot / control lot / shift) ride the same
    # additive pattern for databases created before the pass.
    lab_qc.ensure_lab_qc_columns(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_users(session)
        studio_config.ensure_default_configs(session)
        # Home & Module UX pass: the job-profiles seed (navigation
        # kind, key "job-profiles") follows the same insert-if-absent
        # published-v1 pattern; its ensure lives in app.configstore
        # next to the payload (see ensure_job_profiles_default).
        ensure_job_profiles_default(session)
        # Inventory & Materials pass: the governed inventory taxonomy
        # (kind "inventory", key "taxonomy") seeds by the same
        # insert-if-absent published-v1 pattern.
        ensure_inventory_defaults(session)
        lab.ensure_lab_defaults(session)
        # Lab Depth pass (LAB-D8/D9): governed reagent-QC config
        # (verification criteria, control-lot onboarding, shifts,
        # calibration) seeds published v1, insert-only.
        lab_qc.ensure_lab_reagent_qc_defaults(session)
        # Lab Depth pass (LAB-D2): bench registry (terminology/
        # lab-benches) seeds published v1, insert-only.
        lab_compendium.ensure_lab_compendium_defaults(session)
        # Lab Depth pass (LAB-D10/D11): TAT escalation + critical-
        # notify chase config (terminology/lab-tat) seeds published
        # v1, insert-only. Targets themselves live in W1's directory.
        lab_tat.ensure_lab_tat_defaults(session)
        # Lab Depth pass (LAB-D13): transfusion-service config
        # (T&S validity, reservation, wastage reasons) seeds
        # published v1, insert-only.
        lab_transfusion.ensure_lab_transfusion_defaults(session)
        pophealth.ensure_pophealth_defaults(session)
        billing.ensure_billing_defaults(session)
        pharmacy.ensure_pharmacy_defaults(session)
        pharmacy_ops.ensure_pharmacy_ops_defaults(session)
        boards.ensure_boards_defaults(session)
        orders.ensure_orders_defaults(session)
        documentation.ensure_documentation_defaults(session)
        inpatient.ensure_inpatient_defaults(session)
        access.ensure_access_defaults(session)
        revenue.ensure_revenue_defaults(session)
        interop.ensure_interop_defaults(session)
        engagement.ensure_engagement_defaults(session)
        quality.ensure_quality_defaults(session)
        agents.ensure_agent_defaults(session)
        rules.ensure_rule_defaults(session)
        compliance.ensure_compliance_defaults(session)
        continuum.ensure_continuum_defaults(session)
        people.ensure_people_defaults(session)
        reports.ensure_reports_defaults(session)
        devices.ensure_devices_defaults(session)
        pipelines.ensure_pipelines_defaults(session)
        # Front Door pass: accounts seed after people (the front-desk
        # account links a staff-directory entry), registration config
        # (MRN format) has no dependencies.
        accounts.ensure_account_defaults(session)
        registration.ensure_registration_defaults(session)
        # Phase 15: departments, clinical depth & operations modules.
        lab_specimens.ensure_lab_specimens_defaults(session)
        lab_plates.ensure_lab_plate_defaults(session)
        lab_depth.ensure_lab_depth_defaults(session)
        # Lab Depth pass (LAB-D12): microbiology workup defaults.
        lab_micro.ensure_lab_micro_defaults(session)
        # Lab Depth pass (LAB-D15/D16): POCT deployment profile,
        # competency-gating map and lab QMS programme configs seed
        # published v1, insert-only.
        lab_poct_qms.ensure_lab_poct_qms_defaults(session)
        # Lab Depth pass (LAB-D6/D7): analyser-integration and
        # autoverification governed configs seed published v1,
        # insert-only (autoverification rule sets seed OFF — no
        # bench auto-releases until it publishes its own set).
        lab_analyser.ensure_lab_analyser_defaults(session)
        lab_autoverify.ensure_lab_autoverify_defaults(session)
        # Lab Depth pass (LAB-D14): molecular assay catalogue and
        # run-governance config seeds published v1, insert-only.
        lab_molecular.ensure_lab_molecular_defaults(session)
        # Lab Depth pass (LAB-D3/D4/D5): pre-analytical governed
        # config (receipt checklist, transport rules, storage
        # locations, retention schedule) seeds published v1.
        lab_preanalytical.ensure_lab_preanalytical_defaults(session)
        radiology.ensure_radiology_defaults(session)
        surgery_depth.ensure_surgery_depth_defaults(session)
        support_ops.ensure_support_ops_defaults(session)
        support_services.ensure_support_services_defaults(session)
        care_coordination.ensure_care_coordination_defaults(session)
        access_services.ensure_access_services_defaults(session)
        platform_depth.ensure_platform_depth_defaults(session)
        listmgmt.ensure_listmgmt_defaults(session)
        results_inbox.ensure_results_inbox_defaults(session)
        rfid.ensure_rfid_defaults(session)
        barcodes.ensure_barcodes_defaults(session)
        infection_prevention.ensure_infection_prevention_defaults(session)
        care_management.ensure_care_management_defaults(session)
        # Phase 16 (Intelligence & Research). Directories sync reads the
        # FHIR store, so it seeds after people/fhir data exists; the
        # agent-ecosystem ensures run after agents.ensure_agent_defaults
        # above; esignature precedes research_ops (its snapshot registry
        # is populated at research_ops import, and both seed the Part 11
        # pack through the same helper).
        directories.ensure_directories_defaults(session)
        federation.ensure_federation_defaults(session)
        gateway.ensure_gateway_defaults(session)
        agent_workflows.ensure_agent_workflows_defaults(session)
        agent_validation.ensure_agent_validation_defaults(session)
        innovation.ensure_innovation_defaults(session)
        rag.ensure_rag_defaults(session)
        mcp_a2a.ensure_mcp_a2a_defaults(session)
        imaging_assist.ensure_imaging_assist_defaults(session)
        genomics.ensure_genomics_defaults(session)
        esignature.ensure_esignature_defaults(session)
        research_ops.ensure_research_defaults(session)
        empi.ensure_empi_defaults(session)
        doc_intake.ensure_doc_intake_defaults(session)
        analytics.ensure_analytics_defaults(session)
        maintenance.ensure_maintenance_defaults(session)
        # Workforce, Teams & Panels pass: care teams, privileging,
        # empanelment and the onboarding pipeline. Seeds run after the
        # workforce directory (app.people) and accounts exist; the
        # empanelment seed only lands for patients already in the
        # store (the demo seed), never inventing patients.
        teams.ensure_teams_defaults(session)
        onboarding.ensure_onboarding_defaults(session)
        # Organisation & Registration Placement pass: after the
        # locations config, staff directory and accounts all exist —
        # extend a pre-pass locations hierarchy (governed republication)
        # and backfill placements onto staff/account rows that carry
        # a unit but no placement. Idempotent; silent on later boots.
        org_hierarchy.ensure_org_placement(session)
        # Platform pass note: app.protocols intentionally seeds NO
        # protocol definitions — SOPs are site-governed artefacts, so
        # a fresh deployment has zero published protocols until a site
        # publishes one in Studio (see docs/sqa/PLATFORM_SQA_REPORT.md).
        # RTM STU-04: pack manifests are reference data — load them at
        # boot (idempotent upsert; the Studio handlers also load
        # lazily) so the pack-enablement gate can see declarations
        # from the first request instead of only after a /studio call.
        studio.load_pack_manifests(session)
        # Phase 17 (WS-PACKGATE, BD-01 completion): pack-gating ships
        # ON (the published integration/pack-gating seed in
        # studio_config.ensure_default_configs, above), so the core
        # packs declaring the core application's routes are seeded
        # ENABLED through the governed pack lifecycle — a fresh
        # deployment gates and serves. Insert-only: an operator's
        # later disable of a core pack survives restarts.
        studio.ensure_core_pack_installations(session)

    app = FastAPI(title="OpenCare EHR", version="0.7.0",
                  description=("Open-source, FHIR-native EHR. Phase 1 core EHR "
                               "plus the Studio customization engine: forms, "
                               "workflows, terminology, navigation, branding "
                               "and permissions as versioned, published "
                               "configuration (docs/STUDIO_CUSTOMIZATION.md). "
                               "Phase 2: Lab LIS — order -> specimen -> result "
                               "-> QC on Studio-configured catalog + workflow "
                               "(app.lab). Phase 3: transactional outbox -> "
                               "FHIR-to-OMOP warehouse ETL + aggregate-only "
                               "population health (app.outbox, app.etl, "
                               "app.warehouse, app.pophealth). Phase 4A: "
                               "billing/claims — charge capture from the "
                               "Studio fee schedule, config-driven claim "
                               "lifecycle with one-time appeal, X12 837P "
                               "export (app.billing) — and pharmacy: "
                               "formulary + stock-controlled dispensing "
                               "and BCMA-lite med-admin (app.pharmacy). "
                               "Phase 4B: specialty boards (app.boards) — "
                               "ED board (config-driven tracking states, "
                               "ESI triage, server-computed wait times, "
                               "admit/depart), radiology (ImagingStudy "
                               "from orders, DICOMweb study metadata, "
                               "preliminary -> final report verify, "
                               "external OHIF viewer link from Studio "
                               "integration config) and the surgery board "
                               "(OR rooms as config, room-overlap "
                               "double-booking refused server-side). "
                               "Phase 5: general orders / CPOE — order "
                               "types, lifecycle and order sets as Studio "
                               "config, allergy + duplicate decision "
                               "support, consult Tasks + inbox, and the "
                               "computed MAR (app.orders) — clinical "
                               "documentation: notes (DocumentReference "
                               "author/sign/amend on Studio form "
                               "templates) and Questionnaire-driven "
                               "flowsheets (app.documentation) — and "
                               "deployment-profile packs (solo practice "
                               "/ clinic) whose configuration is applied "
                               "at pack enable time (app.studio). Phase 6: "
                               "inpatient operations (app.inpatient) — "
                               "locations & beds materialised from Studio "
                               "terminology config, the ADT event system "
                               "(A01 admit / A02 transfer / A03 discharge / "
                               "A08 update) over the Encounter lifecycle, "
                               "census + bed board, case management and "
                               "discharge planning (checklist form, "
                               "dispositions, expected-discharges feed), "
                               "and perioperative depth on the surgery "
                               "board: pre-op checklist, anaesthesia record "
                               "+ vitals flowsheet, PACU note type, and a "
                               "server-timestamped case timeline. "
                               "Phase 7: access & full revenue cycle — "
                               "referrals + prior authorisations on "
                               "Studio payer/service/workflow config "
                               "with an auth gate at order signing, "
                               "recorded local-config eligibility "
                               "checks (no live payer EDI), and the "
                               "appointment waitlist (app.access) — "
                               "HIM/coding queues on published ICD-10/"
                               "CPT value sets feeding claim generation "
                               "(optional requireCoding gate), X12 835 "
                               "remittance posting with batch "
                               "exceptions, the denials workqueue with "
                               "rebill, and patient statements + "
                               "recorded payments (app.revenue). "
                               "Phase 8: live interoperability — one "
                               "shared HL7 v2 module (app.hl7), an "
                               "interface outbox fed in-transaction by "
                               "the ADT/order-signing/lab-release "
                               "events, a real MLLP listener service "
                               "(app.mllp_server) routing inbound ORU "
                               "into the existing lab ingest and "
                               "recording inbound ADT as external "
                               "events (never mutating census), a "
                               "retrying/dead-lettering dispatcher, "
                               "public-health ELR from the published "
                               "reportable-conditions list, and FHIR "
                               "bulk ($export-style) NDJSON jobs "
                               "behind the interop.export capability "
                               "(app.interop). Phase 9: specialty "
                               "operations as governed Studio packs "
                               "(backend/packs/specialty-*.pack.json) on "
                               "this same core, plus patient engagement "
                               "— secure messaging on FHIR Communication, "
                               "telehealth visits (external provider "
                               "join link as config data; no video "
                               "stack), self-scheduling over the "
                               "published slot types (app.engagement) — "
                               "a config-defined quality-measure engine "
                               "reusing the population-health evaluator "
                               "over the warehouse (app.quality), and "
                               "the research cohort workbench: snapshot "
                               "count history + audited aggregate-only "
                               "exports (app.pophealth). Phase 10: "
                               "foundation — Studio-managed agents on "
                               "an ADK runtime service (app.agents + "
                               "backend/adk; honest-off with no model), "
                               "a governed rules/automation engine "
                               "(app.rules) carrying the critical-value "
                               "escalation, pack authoring + dashboards "
                               "as config, the compliance framework "
                               "(requirements register, regulatory packs, "
                               "evidence + tracer; app.compliance), and "
                               "the security core: break-the-glass with "
                               "100% post-hoc review (app.privacy), "
                               "field-level envelope encryption of "
                               "message bodies (app.crypto), and a "
                               "config-flagged downtime read-only mode. "
                               "Phase 11: pharmacy department depth "
                               "(app.pharmacy_ops) — lot-level inventory "
                               "and procurement with a DSCSA traceability "
                               "ledger, controlled substances (two-person "
                               "counts, witnessed waste, discrepancy "
                               "resolution), sterile compounding with "
                               "config-computed beyond-use dating, the "
                               "pharmacist verification + DUR work "
                               "queues (renal flag via the rules engine), "
                               "an inert-by-design drug-interaction "
                               "integration point (no knowledge source "
                               "ships), and full five-rights BCMA "
                               "scanning with governed barcode payload "
                               "standards. Phase 12: care-continuum "
                               "operations (app.continuum) — ER "
                               "operations (ESI target timers, split "
                               "flow, boarding with rules-engine "
                               "escalation, STEMI/stroke/trauma "
                               "activation timers, diversion state, "
                               "LWBS capture, results callbacks, and "
                               "the EMTALA screening/transfer-"
                               "certification gate), inpatient "
                               "operations (acuity assignments, rounds, "
                               "I-PASS handoffs, discharge-readiness "
                               "board, EVS turnarounds), outpatient "
                               "operations (session templates with "
                               "overbooking limits, rooming, no-show "
                               "risk from the patient's own history, "
                               "backfill, referral loop closure, an "
                               "SLA inbox, panel care gaps with "
                               "outreach tasks), aftercare (teach-back, "
                               "48-72h call queue, home-health "
                               "tracking, deterministic readmission "
                               "risk, hospital-at-home board), a "
                               "Capacity Command Center (live census + "
                               "deterministic projection, bed requests, "
                               "transfer centre, transport dispatch), "
                               "and Armstrong-pattern safety event "
                               "reporting with confidential filing, "
                               "two-way follow-up and an RCA queue. "
                               "Phase 13: people & data platform — the "
                               "workforce core (staff directory, "
                               "credentials/licence expiry worklist with "
                               "rules-engine renewal alerts, rosters, "
                               "on-call schedules with a who-is-on-call "
                               "lookup; app.people), seeded published "
                               "audience dashboards (clinician, nurse/unit, "
                               "unit manager, department head, executive) "
                               "with a role-aware home landing and live "
                               "analytics endpoints, chart widgets "
                               "(line/bar/gauge/table) in the shared "
                               "dashboard renderer, the governed report "
                               "catalogue with server-side PDF (ReportLab) "
                               "and the printable invoice / receipt / "
                               "patient statement / superbill documents "
                               "on a config letterhead (every print "
                               "audited), the capability-gated export "
                               "centre over named server-defined datasets, "
                               "and the import centre (mapping profiles "
                               "as config, full validation error report, "
                               "all-or-nothing commit; app.reports). "
                               "Phase 14: devices, IoT, lake & pipelines "
                               "— a general device registry with "
                               "calibration gating (an overdue analyser "
                               "cannot release results: refused or "
                               "quarantined at the Phase 8 MLLP gate per "
                               "published config), FHIR Observation + "
                               "MQTT ingestion for monitors and storage "
                               "sensors (all feeds simulated/recorded "
                               "and labelled as such; the MQTT adapter "
                               "is honestly off with no broker "
                               "configured), an immutable raw-zone data "
                               "lake with lineage from every curated "
                               "record back to its raw payload "
                               "(app.devices, app.lake), and governed "
                               "pipelines as Studio config — idempotent "
                               "steps, retry with dead-letter, "
                               "data-quality quarantine, terminology "
                               "normalisation, warehouse load through "
                               "the Phase 3 ETL, lake replay/backfill, "
                               "and an MPI dedup workqueue that never "
                               "auto-merges (app.pipelines)."))
    app.state.engine = engine
    app.state.session_factory = session_factory

    # Browser clients (web app on the Vite dev origin :5173, the nginx
    # production origin :8080, the SMART app) are cross-origin to this API.
    # Without CORS middleware the browser blocks every response even though
    # the API is up — curl/server-side smokes never see it. Origins are
    # configurable via CORS_ORIGINS (comma-separated); the defaults cover
    # local development only.
    cors_origins = [o.strip() for o in os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:8080,http://127.0.0.1:8080,"
        "http://localhost:5174,http://127.0.0.1:5174").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def downtime_guard(request: Request, call_next):
        """Downtime read-only mode (Phase 10, Track K): when the
        PUBLISHED integration/downtime config sets readOnly, write
        methods are refused 503 with the configured notice while reads
        continue — the honest degradation hospitals plan for (their
        paper/BCA fallback). Studio paths stay writable so an operator
        can always turn the flag back off; /health is exempt. The flag
        is read from published config on each write request, like every
        other config consumer in this codebase."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            path = request.url.path
            if path != "/health" and not path.startswith("/studio"):
                try:
                    with session_factory() as guard_session:
                        cfg = published_config(guard_session, "integration",
                                               "downtime")
                    payload = (cfg.payload or {}) if cfg is not None else {}
                    if payload.get("readOnly") is True:
                        notice = payload.get("message") or (
                            "the EHR is in downtime read-only mode")
                        return JSONResponse(
                            status_code=503,
                            content={"detail": (
                                f"Downtime read-only mode: {notice}. "
                                "Writes are refused; reads continue.")})
                except Exception:
                    pass  # a guard failure must never take reads down
        return await call_next(request)

    @app.middleware("http")
    async def pack_gate_guard(request: Request, call_next):
        """Pack-enablement route gating (Platform pass, RTM STU-04;
        app.pack_gate): requests to endpoints declared in a pack
        manifest are refused 403 until an installation of a declaring
        pack is enabled. Phase 17 (BD-01 completion): ON by default —
        the seeded PUBLISHED integration/pack-gating config enables
        it, absent config also means ON, and the core packs are
        seeded enabled through the pack lifecycle, so a fresh
        deployment gates and serves. The escape hatch is governed:
        publish integration/pack-gating {"enabled": false}.
        /health and /studio stay reachable so an operator can always
        enable a pack or publish the gate back off."""
        path = request.url.path
        if path != "/health" and not path.startswith("/studio"):
            try:
                with session_factory() as guard_session:
                    if pack_gate.gating_enabled(guard_session):
                        blocked, packs = pack_gate.declared_endpoint_block(
                            guard_session, request.method, path)
                        if blocked:
                            return JSONResponse(
                                status_code=403,
                                content={"detail": (
                                    "Pack-enablement gating: "
                                    f"{request.method} {path} is declared "
                                    f"by pack(s) {', '.join(packs)} and "
                                    "stays disabled until an installation "
                                    "of a declaring pack is enabled "
                                    "(Studio → Module packs).")})
            except Exception:
                pass  # a guard failure must never take the API down
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "opencare-ehr", "phase": "16"}

    app.include_router(studio.router)
    app.include_router(studio_config.router)
    app.include_router(fhir.router)
    app.include_router(fhir.portal_router)
    app.include_router(lab.router)
    app.include_router(lab_reagents.router)
    app.include_router(lab_qc.router)
    app.include_router(lab_compendium.router)
    app.include_router(lab_tat.router)
    app.include_router(lab_transfusion.router)
    app.include_router(lab_poct_qms.router)
    app.include_router(lab_analyser.router)
    app.include_router(lab_autoverify.router)
    app.include_router(lab_molecular.router)
    app.include_router(lab_preanalytical.router)
    app.include_router(lab_benches.router)
    app.include_router(pophealth.router)
    app.include_router(pophealth.warehouse_router)
    app.include_router(billing.router)
    app.include_router(pharmacy.router)
    app.include_router(pharmacy_ops.router)
    app.include_router(boards.ed_router)
    app.include_router(boards.radiology_router)
    app.include_router(boards.dicomweb_router)
    app.include_router(boards.surgery_router)
    app.include_router(orders.router)
    app.include_router(documentation.notes_router)
    app.include_router(documentation.flowsheets_router)
    app.include_router(inpatient.router)
    app.include_router(access.router)
    app.include_router(revenue.router)
    app.include_router(interop.router)
    app.include_router(engagement.router)
    app.include_router(engagement.engagement_router)
    app.include_router(engagement.portal_router)
    app.include_router(quality.router)
    app.include_router(agents.router)
    app.include_router(rules.router)
    app.include_router(compliance.router)
    app.include_router(privacy.router)
    app.include_router(continuum.router)
    app.include_router(people.router)
    app.include_router(reports.router)
    app.include_router(devices.router)
    app.include_router(pipelines.router)
    app.include_router(accounts.router)
    app.include_router(registration.router)
    # Platform pass route groups: SOP/protocol framework and hot/cold
    # tiering (both governed by published Studio config).
    app.include_router(protocols.router)
    app.include_router(tiering.router)
    # Platform pass route groups: report engine + indicator library.
    app.include_router(report_engine.router)
    app.include_router(indicators.router)
    # Home & Module UX pass: Home card menu data + effective-modules.
    app.include_router(home_cards.router)
    # Platform pass route group: Studio DB platform (adapters,
    # connection profiles, schema designer, transformer workbench).
    app.include_router(dbplatform.router)
    # Platform pass WS10 route groups (Studio full parity): the AI
    # provider registry and the Studio ops surfaces (approvals
    # inbox, organisation read model, data-views registry).
    app.include_router(ai_providers.router)
    app.include_router(studio_ops.router)
    # Phase 15 route groups (departments, clinical depth & operations).
    app.include_router(lab_specimens.router)
    app.include_router(lab_plates.router)
    app.include_router(lab_depth.router)
    app.include_router(lab_micro.router)
    app.include_router(radiology.router)
    app.include_router(surgery_depth.router)
    app.include_router(support_ops.router)
    app.include_router(support_services.router)
    app.include_router(inventory.router)
    app.include_router(care_coordination.router)
    app.include_router(access_services.router)
    app.include_router(platform_depth.router)
    app.include_router(listmgmt.router)
    app.include_router(results_inbox.router)
    app.include_router(rfid.router)
    app.include_router(barcodes.router)
    app.include_router(infection_prevention.router)
    app.include_router(care_management.router)
    # Phase 16 route groups (Intelligence & Research). mcp_a2a's combined
    # router mounts /mcp, /a2a and /.well-known/agent-card.json.
    app.include_router(directories.router)
    app.include_router(federation.router)
    app.include_router(gateway.router)
    app.include_router(agent_workflows.router)
    app.include_router(agent_validation.router)
    app.include_router(ai_governance.router)
    app.include_router(innovation.router)
    app.include_router(rag.router)
    app.include_router(mcp_a2a.router)
    app.include_router(imaging_assist.router)
    app.include_router(genomics.router)
    app.include_router(esignature.router)
    app.include_router(research_ops.router)
    app.include_router(empi.router)
    app.include_router(doc_intake.router)
    app.include_router(analytics.router)
    app.include_router(maintenance.router)
    # Workforce, Teams & Panels pass route groups.
    app.include_router(teams.router)
    app.include_router(onboarding.router)
    app.include_router(org_hierarchy.router)
    app.include_router(session_security.router)
    # Phase 17 route groups: GraphQL aggregation lane (BD-68),
    # realtime fan-out (BD-69), observability /metrics + traces (BD-61).
    app.include_router(graphql_lane.router)
    app.include_router(realtime.router)
    app.include_router(observability.router)
    # Phase 17 WS-COMP route groups: breach operations (BD-55),
    # secure print queue (BD-56), jurisdiction packs (BD-57/58/59),
    # QRDA eCQM export (BD-60).
    app.include_router(breach_ops.router)
    app.include_router(print_queue.router)
    app.include_router(jurisdictions.router)
    app.include_router(qrda_export.router)
    # Phase 17 WS-SEC route groups (BD-51/52/53): KMS key management,
    # privacy-monitoring workqueue, log-redaction boundary.
    app.include_router(keymgmt.router)
    app.include_router(privacy_monitor.router)
    app.include_router(logredact.router)
    return app


app = create_app()
