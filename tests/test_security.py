"""Security review checks (assignment section 51) that are not covered elsewhere.

See also: test_repository_hygiene.py (no secrets in the repository, .gitignore), test_config.py
(secrets never in repr), test_mcp_server.py / test_error_handling.py (server-side authorization,
validated tool arguments, safe error messages), test_cli.py (no keys or traces on screen).
"""

import io
import json
import os
import subprocess
import sys
import urllib.error
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from northstar.database.engine import make_engine, search_path_for, validate_schema
from northstar.database.migrations import apply_migrations
from scripts.check_setup import check_supabase_rest
from tests.mcp_helpers import McpHarness, ToolCallError

ROOT = Path(__file__).resolve().parents[1]


# --- SQL built from configuration is restricted to identifiers ---------------------------------------

@pytest.mark.parametrize("schema", ['public"; DROP TABLE employees; --', "Public", "1abc", "a b", ""])
def test_schema_names_cannot_inject_sql(schema):
    with pytest.raises(ValueError):
        validate_schema(schema)
    with pytest.raises(ValueError):
        search_path_for(schema)


def test_migrations_refuse_invalid_schema_before_any_sql():
    class NoEngine:
        def begin(self):
            raise AssertionError("must not connect")

    with pytest.raises(ValueError):
        apply_migrations(NoEngine(), 'x"; DROP SCHEMA public CASCADE; --')


# --- Supabase public API roles have no privileges on HR data -------------------------------------

@pytest.mark.db
def test_public_api_roles_have_no_table_privileges(db_url):
    admin = make_engine(db_url)
    with admin.begin() as conn:
        for role in ("anon", "authenticated"):           # exist on Supabase; created for plain PostgreSQL
            exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
            if not exists:
                try:
                    conn.execute(text(f"CREATE ROLE {role} NOLOGIN"))
                except Exception:
                    pytest.skip("cannot create API roles on this server")
    schema = f"test_sec_{uuid.uuid4().hex[:8]}"
    try:
        apply_migrations(admin, schema)
        with admin.connect() as conn:
            granted = conn.execute(text("""
                SELECT c.relname, r.rolname
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                CROSS JOIN (VALUES ('anon'), ('authenticated')) AS r(rolname)
                WHERE n.nspname = :s AND c.relkind IN ('r', 'v')
                  AND (has_table_privilege(r.rolname, c.oid, 'SELECT')
                       OR has_table_privilege(r.rolname, c.oid, 'INSERT')
                       OR has_table_privilege(r.rolname, c.oid, 'UPDATE')
                       OR has_table_privilege(r.rolname, c.oid, 'DELETE'))"""), {"s": schema}).all()
            rls_off = conn.execute(text("""
                SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = :s AND c.relkind = 'r' AND NOT c.relrowsecurity"""), {"s": schema}).all()
        assert granted == []
        assert rls_off == []                                 # RLS stays on as the second layer
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


# --- MCP tool arguments are validated by typed schemas ---------------------------------------------

@pytest.mark.db
@pytest.mark.parametrize("tool, args", [
    ("propose_leave_request", {"conversation_id": "not-a-uuid", "leave_type": "ANNUAL",
                               "start_date": "2026-11-02", "end_date": "2026-11-03"}),
    ("propose_leave_request", {"conversation_id": str(uuid.uuid4()), "leave_type": "ANNUAL",
                               "start_date": "2026-11-02", "end_date": "2026-11-03", "comment": "x" * 501}),
    ("list_leave_requests", {"limit": 10_000}),
    ("get_leave_balance", {"year": 1900}),
    ("approve_leave_request", {"request_id": -1}),
])
def test_invalid_tool_arguments_are_rejected(seeded_engine, tool, args):
    with pytest.raises(ToolCallError):
        McpHarness(seeded_engine).call(tool, **args)


# --- service-role key: backend only, never printed --------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_rest_check_sends_key_only_as_headers():
    seen = {}

    def opener(request, timeout):
        seen.update(url=request.full_url, headers=dict(request.header_items()))
        return FakeResponse(json.dumps([{"code": "ANNUAL"}] * 6).encode())

    check = check_supabase_rest("https://proj.supabase.co/", "eyJhbGciOi.service.jwt", opener)
    assert check.ok and "6 leave types" in check.detail
    assert seen["url"] == "https://proj.supabase.co/rest/v1/leave_types?select=code"   # key not in the URL
    assert seen["headers"]["Apikey"] == "eyJhbGciOi.service.jwt"
    assert seen["headers"]["Authorization"] == "Bearer eyJhbGciOi.service.jwt"
    assert "eyJ" not in check.detail


def test_rest_check_new_secret_key_and_failures_do_not_leak():
    def opener(request, timeout):
        assert "Authorization" not in dict(request.header_items())   # sb_secret_ keys are not JWTs
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    check = check_supabase_rest("https://proj.supabase.co", "sb_secret_abc123", opener)
    assert not check.ok and check.detail == "HTTP 401: key rejected" and "sb_secret" not in check.detail
    assert check_supabase_rest(None, None).detail.startswith("skipped")


@pytest.mark.db
def test_check_setup_script_reports_without_secrets(seeded_engine, db_url, db_schema):
    env = dict(os.environ)
    env.update({"DATABASE_URL": db_url, "DB_SCHEMA": db_schema, "GEMINI_API_KEY": "test-key-not-real",
                "EMBEDDING_PROVIDER": "local", "SUPABASE_URL": "", "SUPABASE_SERVICE_ROLE_KEY": "",
                "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"})
    result = subprocess.run([sys.executable, "-m", "scripts.check_setup"], env=env, cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", timeout=120)
    out = result.stdout + result.stderr
    assert "✓ PostgreSQL connection" in out and "✓ pgvector extension" in out
    assert "✓ Migrations: up to date" in out and "✓ Seed data" in out and "✓ Demo identity" in out
    assert "Supabase REST API: skipped" in out
    assert db_url not in out and "test-key-not-real" not in out and "Traceback" not in out
    assert result.returncode == (0 if "required check(s) failed" not in out else 1)


def test_check_setup_unreachable_database_is_safe():
    env = dict(os.environ)
    env.update({"DATABASE_URL": "postgresql://u:secret-pw@127.0.0.1:1/x", "GEMINI_API_KEY": "k",
                "SUPABASE_URL": "", "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"})
    result = subprocess.run([sys.executable, "-m", "scripts.check_setup"], env=env, cwd=ROOT, capture_output=True,
                            text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 1 and "✗ PostgreSQL connection: OperationalError" in result.stdout
    assert "secret-pw" not in result.stdout + result.stderr and "Traceback" not in result.stdout + result.stderr
