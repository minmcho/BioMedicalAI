import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import access, billing, boards, documentation, engagement, fhir, inpatient, interop, lab, lab_benches, orders, outbox, pharmacy, pophealth, quality, revenue, studio, studio_config  # noqa: F401
from .db import Base, make_engine, make_session_factory
from .security import seed_users


def create_app(database_url: str | None = None) -> FastAPI:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_users(session)
        studio_config.ensure_default_configs(session)
        lab.ensure_lab_defaults(session)
        pophealth.ensure_pophealth_defaults(session)
        billing.ensure_billing_defaults(session)
        pharmacy.ensure_pharmacy_defaults(session)
        boards.ensure_boards_defaults(session)
        orders.ensure_orders_defaults(session)
        documentation.ensure_documentation_defaults(session)
        inpatient.ensure_inpatient_defaults(session)
        access.ensure_access_defaults(session)
        revenue.ensure_revenue_defaults(session)
        interop.ensure_interop_defaults(session)
        engagement.ensure_engagement_defaults(session)
        quality.ensure_quality_defaults(session)

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
                               "exports (app.pophealth)."))
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

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "opencare-ehr", "phase": "9"}

    app.include_router(studio.router)
    app.include_router(studio_config.router)
    app.include_router(fhir.router)
    app.include_router(fhir.portal_router)
    app.include_router(lab.router)
    app.include_router(lab_benches.router)
    app.include_router(pophealth.router)
    app.include_router(pophealth.warehouse_router)
    app.include_router(billing.router)
    app.include_router(pharmacy.router)
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
    return app


app = create_app()
