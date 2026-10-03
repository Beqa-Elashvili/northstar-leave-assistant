"""Shared pytest fixtures.

Database tests never touch application data: each test session creates its own
temporary schema (`test_<random>`), applies the migrations there and drops it at the end.

Database selection:
1. `TEST_DATABASE_URL` (e.g. the Supabase project) if set;
2. otherwise an embedded PostgreSQL + pgvector started with `pgserver` (dev dependency);
3. otherwise database tests are skipped.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

# Deterministic settings for every test, independent of the developer's .env.
os.environ["APP_TODAY"] = "2026-10-19"
os.environ["APP_TIMEZONE"] = "Asia/Tbilisi"
os.environ.setdefault("DEMO_EMPLOYEE_ID", "E1001")
# The core suite never calls external AI services.
os.environ["EMBEDDING_PROVIDER"] = "local"


def _database_url() -> str | None:
    from dotenv import dotenv_values

    project_env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    url = os.environ.get("TEST_DATABASE_URL") or project_env.get("TEST_DATABASE_URL")
    if url:
        return url
    try:
        import pgserver
    except ImportError:
        return None
    data_dir = Path(tempfile.gettempdir()) / "northstar_pgserver"
    server = pgserver.get_server(data_dir, cleanup_mode=None)
    return server.get_uri()


@pytest.fixture(scope="session")
def db_url() -> str:
    url = _database_url()
    if not url:
        pytest.skip("no PostgreSQL available (set TEST_DATABASE_URL or install pgserver)")
    return url


@pytest.fixture(scope="session")
def db_schema(db_url):
    """A fresh, migrated schema for the whole test session; dropped afterwards."""
    from northstar.database.engine import make_engine
    from northstar.database.migrations import apply_migrations

    schema = f"test_{uuid.uuid4().hex[:10]}"
    admin = make_engine(db_url)
    apply_migrations(admin, schema)
    yield schema
    with admin.begin() as conn:
        conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
    admin.dispose()


@pytest.fixture(scope="session")
def engine(db_url, db_schema):
    from northstar.database.engine import make_engine

    eng = make_engine(db_url, db_schema)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def seed_data():
    from zoneinfo import ZoneInfo

    from northstar.config import DATA_DIR
    from northstar.database.csv_loader import load_seed_data

    return load_seed_data(DATA_DIR, ZoneInfo("Asia/Tbilisi"))


@pytest.fixture()
def seeded_engine(engine, seed_data):
    """The test schema reset to exactly the supplied CSV snapshot before each test."""
    from northstar.database.seed import seed_database

    seed_database(engine, seed_data, reset=True)
    return engine


@pytest.fixture()
def session(seeded_engine):
    from sqlalchemy.orm import Session

    with Session(seeded_engine, expire_on_commit=False) as s:
        yield s
        s.rollback()
