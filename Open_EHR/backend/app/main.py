from fastapi import FastAPI

from . import billing, boards, fhir, lab, outbox, pharmacy, pophealth, studio, studio_config  # noqa: F401
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

    app = FastAPI(title="OpenCare EHR", version="0.6.0",
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
                               "double-booking refused server-side)."))
    app.state.engine = engine
    app.state.session_factory = session_factory

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "opencare-ehr", "phase": "4b"}

    app.include_router(studio.router)
    app.include_router(studio_config.router)
    app.include_router(fhir.router)
    app.include_router(fhir.portal_router)
    app.include_router(lab.router)
    app.include_router(pophealth.router)
    app.include_router(pophealth.warehouse_router)
    app.include_router(billing.router)
    app.include_router(pharmacy.router)
    app.include_router(boards.ed_router)
    app.include_router(boards.radiology_router)
    app.include_router(boards.dicomweb_router)
    app.include_router(boards.surgery_router)
    return app


app = create_app()
