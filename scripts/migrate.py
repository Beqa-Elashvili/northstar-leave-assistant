"""Create or upgrade the database schema.

Usage:
    python -m scripts.migrate
"""

from __future__ import annotations

import sys

from northstar.config import ConfigurationError, get_settings
from northstar.database.engine import get_engine
from northstar.database.migrations import MigrationError, apply_migrations


def main() -> int:
    try:
        settings = get_settings()
        applied = apply_migrations(get_engine(), settings.db_schema)
    except (ConfigurationError, MigrationError) as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # connection problems etc.; never print the URL
        print(f"Migration failed: {type(exc).__name__}. Check DATABASE_URL and network access.", file=sys.stderr)
        return 1
    if applied:
        print(f"Applied migrations to schema '{settings.db_schema}': {', '.join(applied)}")
    else:
        print(f"Schema '{settings.db_schema}' is already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
