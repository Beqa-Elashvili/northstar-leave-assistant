"""Verify that the local setup is complete before running the CLI (read-only).

Usage:
    python -m scripts.check_setup

Checks configuration, the PostgreSQL connection, pgvector, applied migrations, seeded data,
ingested documents, the demo identity and, when SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are
set, the Supabase project through its REST API.

The service-role key is used only here, server-side, as a request header. It is never printed,
logged or passed to the CLI. Output contains no connection strings or keys.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import func, select, text

from northstar.config import Settings, get_settings
from northstar.database.engine import get_session_factory, session_scope
from northstar.database.migrations import load_migrations
from northstar.database.models import Employee, LeaveEntitlement, LeaveRequest, LeaveType, PublicHoliday
from northstar.domain.errors import DomainError
from northstar.rag.embeddings import get_embedding_provider
from northstar.rag.retrieval import RagNotReady, Retriever
from northstar.services.authorization import Principal, Role, verify_principal


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True


def check_configuration(settings: Settings) -> list[Check]:
    checks = [Check("DATABASE_URL", settings.database_url is not None, "" if settings.database_url else "not set")]
    # The CLI always needs the LLM (and Gemini embeddings unless EMBEDDING_PROVIDER=local).
    checks.append(Check("GEMINI_API_KEY", settings.gemini_api_key is not None,
                        "" if settings.gemini_api_key else "not set"))
    checks.append(Check("DEMO_EMPLOYEE_ID", bool(settings.demo_employee_id), settings.demo_employee_id or "not set"))
    checks.append(Check("APP_TODAY", True, settings.app_today.isoformat()))
    return checks


def check_database(settings: Settings) -> list[Check]:
    factory = get_session_factory()
    checks: list[Check] = []
    with session_scope(factory) as session:
        version = session.execute(text("SHOW server_version")).scalar_one()
        checks.append(Check("PostgreSQL connection", True, f"PostgreSQL {version}"))
        vector = session.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
        checks.append(Check("pgvector extension", vector is not None, f"version {vector}" if vector else "missing"))

        expected = {m.name: m.checksum for m in load_migrations()}
        try:
            applied = dict(session.execute(text("SELECT name, checksum FROM schema_migrations")).all())
        except Exception:
            session.rollback()
            applied = {}
        missing = sorted(set(expected) - set(applied))
        changed = sorted(n for n in expected if n in applied and applied[n] != expected[n])
        detail = "up to date" if not (missing or changed) else \
            f"pending: {', '.join(missing) or '-'}; changed: {', '.join(changed) or '-'} (run: python -m scripts.migrate)"
        checks.append(Check("Migrations", not (missing or changed), detail))
        if missing:
            return checks

        counts = {name: session.scalar(select(func.count()).select_from(model)) for name, model in (
            ("employees", Employee), ("leave_types", LeaveType), ("leave_entitlements", LeaveEntitlement),
            ("leave_requests", LeaveRequest), ("public_holidays", PublicHoliday))}
        seeded = all(counts.values())
        checks.append(Check("Seed data", seeded, ", ".join(f"{k}={v}" for k, v in counts.items())
                            + ("" if seeded else " (run: python -m scripts.seed_database)")))

        if settings.demo_employee_id:
            try:
                verify_principal(session, Principal(Role.EMPLOYEE, settings.demo_employee_id))
                checks.append(Check("Demo identity", True, f"{settings.demo_employee_id} is an active employee"))
            except DomainError:
                checks.append(Check("Demo identity", False, f"{settings.demo_employee_id} is not an active employee"))

    try:
        retriever = Retriever(factory, get_embedding_provider(settings))
        with session_scope(factory) as session:
            retriever.check_ready(session)
        checks.append(Check("Policy documents (RAG)", True, "ingested with the configured embedding model"))
    except RagNotReady as exc:
        checks.append(Check("Policy documents (RAG)", False, f"{exc}"))
    except Exception as exc:  # e.g. no Gemini key
        checks.append(Check("Policy documents (RAG)", False, type(exc).__name__))
    return checks


Opener = Callable[..., object]


def check_supabase_rest(url: str | None, service_key: str | None, opener: Opener = urllib.request.urlopen) -> Check:
    """Reach the project's REST API with the service-role key (backend only) and read leave_types."""
    if not url or not service_key:
        return Check("Supabase REST API", True, "skipped (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set)",
                     required=False)
    headers = {"apikey": service_key, "Accept": "application/json"}
    if service_key.startswith("eyJ"):  # legacy JWT service_role key; new sb_secret_ keys use only `apikey`
        headers["Authorization"] = f"Bearer {service_key}"
    request = urllib.request.Request(f"{url.rstrip('/')}/rest/v1/leave_types?select=code", headers=headers)
    try:
        with opener(request, timeout=15) as response:
            rows = json.loads(response.read().decode("utf-8"))
        return Check("Supabase REST API", True, f"project reachable, {len(rows)} leave types visible to the "
                                                 "service role", required=False)
    except urllib.error.HTTPError as exc:
        hint = {401: "key rejected", 403: "key rejected", 404: "table not exposed (DB_SCHEMA not in API schemas?)"}
        return Check("Supabase REST API", False, f"HTTP {exc.code}: {hint.get(exc.code, 'unexpected response')}",
                     required=False)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return Check("Supabase REST API", False, f"not reachable ({type(exc).__name__})", required=False)


def run_checks(settings: Settings) -> list[Check]:
    checks = check_configuration(settings)
    if settings.database_url is not None:
        try:
            checks += check_database(settings)
        except Exception as exc:  # never print the URL or driver message (may contain the host/user)
            checks.append(Check("PostgreSQL connection", False,
                                f"{type(exc).__name__} - check DATABASE_URL and network access"))
    key = settings.supabase_service_role_key.get_secret_value() if settings.supabase_service_role_key else None
    checks.append(check_supabase_rest(settings.supabase_url, key))
    return checks


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        print(f"✗ Configuration: invalid values in .env for {', '.join(fields)}")
        return 1
    checks = run_checks(settings)
    for c in checks:
        mark = "✓" if c.ok else ("✗" if c.required else "!")
        print(f"{mark} {c.name}: {c.detail}" if c.detail else f"{mark} {c.name}")
    failed = [c for c in checks if c.required and not c.ok]
    print("\nSetup OK — start the assistant with: python -m northstar.cli" if not failed
          else f"\n{len(failed)} required check(s) failed.")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
