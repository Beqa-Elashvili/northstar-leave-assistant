"""Create/upgrade the schema and import the supplied CSV files.

Usage:
    python -m scripts.seed_database           # idempotent upsert, keeps requests created later
    python -m scripts.seed_database --reset   # reload exactly the CSV snapshot (2026-10-19)
"""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from northstar.config import DATA_DIR, ConfigurationError, get_settings, invalid_settings_message
from northstar.database.csv_loader import CsvValidationError, load_seed_data
from northstar.database.engine import get_engine
from northstar.database.migrations import MigrationError, apply_migrations
from northstar.database.seed import seed_database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="empty HR tables and reload the CSV snapshot")
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
        data = load_seed_data(DATA_DIR, settings.tz)  # validated before touching the database
        engine = get_engine()
        applied = apply_migrations(engine, settings.db_schema)
        summary = seed_database(engine, data, reset=args.reset)
    except (ConfigurationError, MigrationError, CsvValidationError) as exc:
        print(f"Seeding failed: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"Seeding failed: {invalid_settings_message(exc)}", file=sys.stderr)
        return 1
    except Exception as exc:  # never print connection strings
        print(f"Seeding failed: {type(exc).__name__}. Check DATABASE_URL and network access.", file=sys.stderr)
        return 1

    if applied:
        print(f"Applied migrations: {', '.join(applied)}")
    mode = "reset + reload" if args.reset else "upsert"
    print(f"Imported into schema '{settings.db_schema}' ({mode}):")
    print(f"  employees           {summary.employees}")
    print(f"  leave types         {summary.leave_types}")
    print(f"  leave entitlements  {summary.entitlements}")
    print(f"  leave requests      {summary.requests}")
    print(f"  public holidays     {summary.holidays}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
