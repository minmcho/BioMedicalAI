import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase


class Base(DeclarativeBase):
    pass


def default_database_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite:///./openehr.db")


def make_engine(database_url: str | None = None):
    url = database_url or default_database_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    kwargs: dict = {}
    if not url.startswith("sqlite"):
        # Server engines (PostgreSQL/MariaDB/...): size the pool for
        # concurrent serving. Phase 17 load evidence (WS-LOADDR) showed
        # SQLAlchemy's defaults (5 + 10 overflow = 15) wedge the app at
        # 20 concurrent clients — checkouts timed out and the server
        # never recovered. Pool sizing is deployment configuration:
        # overridable via environment, never hardcoded per-site.
        kwargs = {
            "pool_size": int(os.environ.get("OPENCARE_DB_POOL_SIZE", "20")),
            "max_overflow": int(os.environ.get("OPENCARE_DB_MAX_OVERFLOW", "20")),
            "pool_timeout": int(os.environ.get("OPENCARE_DB_POOL_TIMEOUT", "30")),
            "pool_recycle": int(os.environ.get("OPENCARE_DB_POOL_RECYCLE", "1800")),
            "pool_pre_ping": True,
        }
    return create_engine(url, connect_args=connect_args, future=True, **kwargs)


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
