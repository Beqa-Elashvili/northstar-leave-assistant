"""Minimal, transparent SQL migration runner.

Applies `migrations/NNN_*.sql` in order, each in its own transaction, and records the
file name and SHA-256 in `schema_migrations`. An already-applied file whose content has
changed is reported as an error instead of being silently re-run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, text

from northstar.config import MIGRATIONS_DIR
from northstar.database.engine import search_path_for, validate_schema


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        # Line endings are normalised so a CRLF checkout (Git on Windows) has the same checksum.
        return hashlib.sha256(self.sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise MigrationError(f"no migration files found in {directory}")
    return [Migration(f.name, f.read_text(encoding="utf-8")) for f in files]


def apply_migrations(engine: Engine, schema: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Create the schema if needed and apply pending migrations. Returns the names applied."""
    validate_schema(schema)
    migrations = load_migrations(directory)
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        conn.execute(text('CREATE SCHEMA IF NOT EXISTS "extensions"'))
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA "extensions"'))
        conn.execute(
            text(
                f'CREATE TABLE IF NOT EXISTS "{schema}".schema_migrations ('
                " name text PRIMARY KEY, checksum char(64) NOT NULL,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
        )
        conn.execute(text(f'ALTER TABLE "{schema}".schema_migrations ENABLE ROW LEVEL SECURITY'))
        applied = dict(conn.execute(text(f'SELECT name, checksum FROM "{schema}".schema_migrations')).all())

    newly_applied: list[str] = []
    for migration in migrations:
        if migration.name in applied:
            if applied[migration.name].strip() != migration.checksum:
                raise MigrationError(f"{migration.name} was modified after it was applied")
            continue
        with engine.begin() as conn:
            # Unqualified names in the migration must land in the target schema,
            # regardless of the connection's default search_path.
            conn.exec_driver_sql(f"SET LOCAL search_path TO {search_path_for(schema)}")
            # Raw DBAPI cursor without parameters: '%' in SQL files (e.g. format('%I')) is taken literally.
            with conn.connection.cursor() as cursor:
                cursor.execute(migration.sql)
            conn.execute(
                text(f'INSERT INTO "{schema}".schema_migrations (name, checksum) VALUES (:n, :c)'),
                {"n": migration.name, "c": migration.checksum},
            )
        newly_applied.append(migration.name)
    return newly_applied
