"""The agent's only path to leave data: a fixed set of MCP tools called through an MCP client.

The LLM never chooses tool names or builds arguments directly; the agent maps intents to
these methods. Approve / reject / cancel are deliberately not exposed to the employee
assistant (policy 4.8, 12.3) — and the server would refuse them anyway.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from mcp.client.client import Client

EMPLOYEE_ASSISTANT_TOOLS = frozenset({
    "get_my_profile", "list_leave_types", "get_leave_balance", "list_leave_requests",
    "propose_leave_request", "create_leave_request", "decline_leave_proposal",
})


class ToolFailure(Exception):
    """A tool returned an error. `message` is the server's Georgian, user-safe text."""

    def __init__(self, code: str, message: str, article: str | None = None, details: dict | None = None):
        super().__init__(code)
        self.code, self.message, self.article, self.details = code, message, article, details or {}


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    ok: bool


@dataclass
class McpLeaveTools:
    client: Client
    calls: list[ToolCallRecord] = field(default_factory=list)

    async def _call(self, name: str, **arguments: Any) -> dict:
        if name not in EMPLOYEE_ASSISTANT_TOOLS:
            raise ToolFailure("tool_not_allowed", "ეს მოქმედება ასისტენტისთვის ხელმისაწვდომი არ არის.")
        clean = {k: (v.isoformat() if isinstance(v, date) else str(v) if isinstance(v, uuid.UUID) else v)
                 for k, v in arguments.items() if v is not None}
        try:
            result = await self.client.call_tool(name, clean)
        except Exception:
            self.calls.append(ToolCallRecord(name, clean, False))
            raise ToolFailure("mcp_unavailable", "HR სისტემასთან (MCP სერვერთან) კავშირი ვერ მოხერხდა. სცადეთ მოგვიანებით.")
        self.calls.append(ToolCallRecord(name, clean, not result.is_error))
        if result.is_error:
            text = result.content[0].text if result.content else ""
            payload = _parse_error(text)
            raise ToolFailure(payload.get("code") or "tool_error",
                              payload.get("message") or "მოთხოვნის დამუშავება ვერ მოხერხდა.",
                              payload.get("article"), payload.get("details"))
        return result.structured_content or {}

    async def profile(self) -> dict:
        return await self._call("get_my_profile")

    async def leave_types(self) -> list[dict]:
        return (await self._call("list_leave_types"))["leave_types"]

    async def balance(self, *, year: int | None = None, leave_type: str | None = None,
                      employee_id: str | None = None) -> dict:
        return await self._call("get_leave_balance", year=year, leave_type=leave_type, employee_id=employee_id)

    async def requests(self, *, statuses: list[str] | None = None, leave_type: str | None = None,
                       date_from: date | None = None, date_to: date | None = None,
                       employee_id: str | None = None) -> dict:
        return await self._call("list_leave_requests", status=statuses, leave_type=leave_type,
                                date_from=date_from, date_to=date_to, employee_id=employee_id)

    async def propose(self, *, conversation_id: uuid.UUID, leave_type: str, start_date: date, end_date: date,
                      comment: str | None, sick_period_known_in_advance: bool) -> dict:
        return await self._call("propose_leave_request", conversation_id=conversation_id, leave_type=leave_type,
                                start_date=start_date, end_date=end_date, comment=comment,
                                sick_period_known_in_advance=sick_period_known_in_advance)

    async def create(self, proposal_id: str) -> dict:
        return await self._call("create_leave_request", proposal_id=proposal_id)

    async def decline(self, proposal_id: str) -> dict:
        return await self._call("decline_leave_proposal", proposal_id=proposal_id)


def _parse_error(text: str) -> dict:
    start = text.find("{")
    if start >= 0:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            pass
    return {}
