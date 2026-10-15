from fastapi import FastAPI

from . import fhir, studio
from .db import Base, make_engine, make_session_factory
from .security import seed_users


def create_app(database_url: str | None = None) -> FastAPI:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_users(session)

    app = FastAPI(title="OpenCare EHR", version="0.2.0",
                  description="Open-source, FHIR-native EHR (Phase 1: core EHR MVP).")
    app.state.engine = engine
    app.state.session_factory = session_factory

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "opencare-ehr", "phase": "1"}

    app.include_router(studio.router)
    app.include_router(fhir.router)
    app.include_router(fhir.portal_router)
    return app


app = create_app()
