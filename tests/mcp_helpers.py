"""Helpers for calling the MCP server through a real MCP client in tests."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import anyio
from mcp.client.client import Client
from sqlalchemy.orm import sessionmaker

from northstar.clock import FrozenClock
from northstar.mcp.server import build_server
from northstar.services.authorization import Principal, Role

TZ = ZoneInfo("Asia/Tbilisi")
CLOCK = FrozenClock(datetime(2026, 10, 19, 11, 0, tzinfo=TZ))


class ToolCallError(Exception):
    def __init__(self, payload: dict):
        super().__init__(payload.get("code"))
        self.payload = payload
        self.code = payload.get("code")


def parse_error(text: str) -> dict:
    start = text.find("{")
    if start >= 0:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            pass
    return {"code": "tool_error", "message": text}


class McpHarness:
    """Synchronous facade: each call opens an MCP client session to an in-memory server."""

    def __init__(self, engine, employee_id: str = "E1001", role: Role = Role.EMPLOYEE, clock=CLOCK):
        self.server = build_server(Principal(role, employee_id), sessionmaker(bind=engine, expire_on_commit=False),
                                   clock)

    def call(self, tool: str, **arguments):
        async def go():
            async with Client(self.server) as client:
                return await client.call_tool(tool, arguments)

        result = anyio.run(go)
        if result.is_error:
            raise ToolCallError(parse_error(result.content[0].text if result.content else ""))
        return result.structured_content

    def list_tools(self):
        async def go():
            async with Client(self.server) as client:
                return (await client.list_tools()).tools

        return anyio.run(go)
