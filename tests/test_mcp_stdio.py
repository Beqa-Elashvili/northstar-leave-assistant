"""The MCP server as a real subprocess over the stdio transport (as the CLI uses it)."""

import os
import sys
import uuid
from pathlib import Path

import anyio
import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[1]


def server_params(db_url, db_schema, *args):
    env = dict(os.environ)
    env.update({"DATABASE_URL": db_url, "DB_SCHEMA": db_schema, "APP_TODAY": "2026-10-19",
                "DEMO_EMPLOYEE_ID": "E1001", "PYTHONIOENCODING": "utf-8"})
    return StdioServerParameters(command=sys.executable, args=["-m", "northstar.mcp.server", *args],
                                 env=env, cwd=str(ROOT))


def test_stdio_employee_session(seeded_engine, db_url, db_schema):
    async def go():
        async with Client(server_params(db_url, db_schema)) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            profile = (await client.call_tool("get_my_profile", {})).structured_content
            balance = (await client.call_tool("get_leave_balance", {"leave_type": "ANNUAL"})).structured_content
            denied = await client.call_tool("approve_leave_request", {"request_id": 5})
            proposal = (await client.call_tool("propose_leave_request", {
                "conversation_id": str(uuid.uuid4()), "leave_type": "ANNUAL",
                "start_date": "2026-10-27", "end_date": "2026-10-30"})).structured_content
            created = (await client.call_tool("create_leave_request",
                                              {"proposal_id": proposal["proposal_id"]})).structured_content
            return tools, profile, balance, denied, created

    tools, profile, balance, denied, created = anyio.run(go)
    assert len(tools) == 10
    assert profile["employee_id"] == "E1001"
    assert balance["balances"][0]["available_days"] == 10
    assert denied.is_error and "permission_denied" in denied.content[0].text
    assert created["request"]["status"] == "pending" and created["request"]["created_via"] == "assistant"


def test_stdio_rejects_unknown_identity(seeded_engine, db_url, db_schema):
    import subprocess

    params = server_params(db_url, db_schema, "--employee-id", "E9999")
    proc = subprocess.run([params.command, *params.args], env=params.env, cwd=params.cwd,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 2
    assert "postgresql://" not in proc.stderr  # never leaks the connection string
