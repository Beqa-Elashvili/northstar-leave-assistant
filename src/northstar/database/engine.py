"""SQLAlchemy engine/session factory for Supabase PostgreSQL.

`search_path` is set with a plain `SET` on every new connection instead of the libpq
`options` startup parameter, because the Supabase connection pooler does not accept it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from northstar.config import SCHEMA_RE, get_settings, require_database_url


def validate_schema(schema: str) -> str:
    """Schema names cannot be bound as SQL parameters, so only plain identifiers are accepted."""
    if not SCHEMA_RE.fullmatch(schema):
        raise ValueError("invalid schema name (expected a lowercase SQL identifier)")
    return schema


def search_path_for(schema: str) -> str:
    validate_schema(schema)
    # `extensions` is where Supabase installs pgvector; harmless if it does not exist.
    parts = [schema, "public", "extensions"]
    return ", ".join(dict.fromkeys(f'"{p}"' for p in parts))


def make_engine(url: str, schema: str = "public") -> Engine:
    sa_url = make_url(url)
    if sa_url.drivername in ("postgres", "postgresql"):
        sa_url = sa_url.set(drivername="postgresql+psycopg2")
    engine = create_engine(
        sa_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"connect_timeout": 15, "application_name": "northstar-leave-assistant"},
    )
    path = search_path_for(schema)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_connection, _record):  # pragma: no cover - exercised via DB tests
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"SET search_path TO {path}")
        dbapi_connection.commit()

    return engine


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = make_engine(require_database_url(settings), settings.db_schema)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any error."""
    session = (factory or get_session_factory())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
