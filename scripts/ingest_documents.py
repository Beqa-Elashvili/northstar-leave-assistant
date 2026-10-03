"""Ingest the policy documents into the pgvector store (Supabase).

Usage:
    python -m scripts.ingest_documents            # only new/changed documents
    python -m scripts.ingest_documents --force    # re-embed everything
"""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from northstar.config import DOCUMENTS_DIR, ConfigurationError, get_settings, invalid_settings_message
from northstar.database.engine import get_engine, get_session_factory
from northstar.database.migrations import MigrationError, apply_migrations
from northstar.rag.embeddings import EmbeddingError, get_embedding_provider
from northstar.rag.ingest import ingest_documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="re-process documents even if unchanged")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
        provider = get_embedding_provider(settings, log=print)
        apply_migrations(get_engine(), settings.db_schema)
        print(f"Ingesting {DOCUMENTS_DIR.name}/ with {provider.signature} ...")
        report = ingest_documents(get_session_factory(), provider, DOCUMENTS_DIR, force=args.force, log=print)
    except (ConfigurationError, MigrationError, EmbeddingError) as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"Ingestion failed: {invalid_settings_message(exc)}", file=sys.stderr)
        return 1
    except Exception as exc:  # never print connection strings or keys
        print(f"Ingestion failed: {type(exc).__name__}. Check DATABASE_URL / network access.", file=sys.stderr)
        return 1
    total = sum(report.ingested.values())
    print(f"Done: {len(report.ingested)} ingested ({total} chunks), {len(report.skipped)} unchanged, "
          f"{len(report.removed)} removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
