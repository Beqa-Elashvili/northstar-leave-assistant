"""Northstar leave MCP server.

The server is the only component that changes leave data. Every tool call:
1. opens one database transaction;
2. re-verifies the principal the process was started for (identity is never a tool argument);
3. delegates to `LeaveService`, where authorization and the policy rules are enforced;
4. returns a typed (structured) result, or a `ToolError` whose text is a JSON object
   `{"code", "message", "article", "details"}` with a Georgian, user-safe message.

Run (stdio transport):
    python -m northstar.mcp.server                          # employee = DEMO_EMPLOYEE_ID
    python -m northstar.mcp.server --role hr --employee-id E1007
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from collections.abc import Callable
from datetime import date
from typing import Annotated, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from northstar.clock import Clock, default_clock
from northstar.config import get_settings
from northstar.database.engine import get_session_factory, session_scope
from northstar.domain.errors import DomainError
from northstar.domain.models import (
    BalanceList,
    CreateRequestOut,
    DecisionOut,
    EmployeeProfile,
    LeaveRequestList,
    LeaveTypeList,
    ProposalOut,
    ProposalStatusOut,
)
from northstar.services.authorization import Principal, Role, verify_principal
from northstar.services.leave_requests import LeaveService

logger = logging.getLogger("northstar.mcp")

T = TypeVar("T")
LeaveTypeCode = Literal["ANNUAL", "SICK", "UNPAID", "BEREAVEMENT", "STUDY", "PARENTAL"]
Status = Literal["pending", "approved", "rejected", "cancelled"]
EmployeeId = Annotated[str, Field(pattern=r"^[Ee]\d{4}$", description="Employee ID, e.g. E1001")]

INSTRUCTIONS = (
    "Northstar Services leave management. The caller's identity is fixed by the server session. "
    "To create a leave request: call propose_leave_request, show the proposal to the employee, and only after "
    "the employee explicitly confirms call create_leave_request with the proposal_id. "
    "Approve/reject/cancel require the HR role."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
HR_DECISION = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def _tool_error(payload: dict) -> ToolError:
    return ToolError(json.dumps(payload, ensure_ascii=False, default=str))


def build_server(
    principal: Principal,
    session_factory: sessionmaker[Session] | None = None,
    clock: Clock | None = None,
) -> MCPServer:
    clock = clock or default_clock()
    server = MCPServer(
        name="northstar-leave",
        title="Northstar Services – Leave Management",
        instructions=INSTRUCTIONS,
        version="1.0.0",
        log_level="WARNING",
    )

    def run(operation: Callable[[LeaveService], T]) -> T:
        try:
            with session_scope(session_factory or get_session_factory()) as session:
                verify_principal(session, principal)
                return operation(LeaveService(session, clock))
        except DomainError as exc:
            raise _tool_error(exc.to_dict()) from None
        except SQLAlchemyError:
            logger.exception("database error")
            raise _tool_error({"code": "database_error", "message": "მონაცემთა ბაზასთან კავშირი ვერ მოხერხდა. "
                               "სცადეთ მოგვიანებით.", "article": None, "details": {}}) from None

    # --- read tools ----------------------------------------------------------------------------

    @server.tool(annotations=READ_ONLY, description="Profile of the authenticated caller (no arguments).")
    def get_my_profile() -> EmployeeProfile:
        return run(lambda s: s.profile(principal))

    @server.tool(annotations=READ_ONLY,
                 description="All six leave types with their day unit, annual limit and whether the HR portal "
                             "(self_service) and the HR assistant (assistant_supported) can create them.")
    def list_leave_types() -> LeaveTypeList:
        return run(lambda s: s.list_leave_types())

    @server.tool(annotations=READ_ONLY,
                 description="Leave balance (entitled, carried over, approved, pending, available days) for a year "
                             "and optionally one leave type. Employees can only read their own balance; "
                             "employee_id may be omitted (defaults to the caller).")
    def get_leave_balance(
        year: Annotated[int | None, Field(ge=2000, le=2100, description="Leave year; default: current year")] = None,
        leave_type: Annotated[LeaveTypeCode | None, Field(description="Omit for all types")] = None,
        employee_id: EmployeeId | None = None,
    ) -> BalanceList:
        return run(lambda s: s.balances(principal, employee_id, year, leave_type))

    @server.tool(annotations=READ_ONLY,
                 description="List leave requests filtered by employee, status, leave type and a date range "
                             "(requests overlapping [date_from, date_to]). Employees only see their own requests.")
    def list_leave_requests(
        employee_id: EmployeeId | None = None,
        status: Annotated[list[Status] | None, Field(description="One or more statuses")] = None,
        leave_type: LeaveTypeCode | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> LeaveRequestList:
        return run(lambda s: s.list_requests(principal, employee_id, status, leave_type, date_from, date_to, limit))

    # --- creation: propose -> explicit confirmation -> create ---------------------------------

    @server.tool(annotations=WRITE_IDEMPOTENT,
                 description="Validate a leave request for the caller against all policy rules and, if valid, store "
                             "a proposal awaiting the employee's explicit confirmation. Does NOT create the request. "
                             "Returns outcome awaiting_confirmation (with proposal_id), needs_input or not_allowed "
                             "(with Georgian explanations, policy articles and redirect channel).")
    def propose_leave_request(
        conversation_id: Annotated[uuid.UUID, Field(description="ID of the current conversation")],
        leave_type: LeaveTypeCode,
        start_date: date,
        end_date: date,
        comment: Annotated[str | None, Field(max_length=500, description="Short reason (required for UNPAID); "
                                                                          "never health details")] = None,
        sick_period_known_in_advance: Annotated[bool, Field(
            description="SICK with future dates: the employee confirmed the period is known in advance")] = False,
    ) -> ProposalOut:
        return run(lambda s: s.propose(principal, conversation_id, leave_type, start_date, end_date, comment,
                                       sick_period_known_in_advance))

    @server.tool(annotations=WRITE_IDEMPOTENT,
                 description="Create the leave request of a proposal the employee has explicitly confirmed. The new "
                             "request is pending (not approved) and created_via=assistant. Idempotent: confirming the "
                             "same proposal again returns the original request instead of creating a duplicate.")
    def create_leave_request(proposal_id: uuid.UUID) -> CreateRequestOut:
        return run(lambda s: s.confirm(principal, proposal_id))

    @server.tool(annotations=WRITE_IDEMPOTENT, description="Discard a proposal the employee declined.")
    def decline_leave_proposal(proposal_id: uuid.UUID) -> ProposalStatusOut:
        return run(lambda s: s.decline(principal, proposal_id))

    # --- HR decisions --------------------------------------------------------------------------

    @server.tool(annotations=HR_DECISION, description="HR only: approve a pending leave request.")
    def approve_leave_request(request_id: Annotated[int, Field(ge=1)],
                              comment: Annotated[str | None, Field(max_length=500)] = None) -> DecisionOut:
        return run(lambda s: s.approve(principal, request_id, comment))

    @server.tool(annotations=HR_DECISION, description="HR only: reject a pending leave request with a reason.")
    def reject_leave_request(request_id: Annotated[int, Field(ge=1)],
                             reason: Annotated[str, Field(min_length=1, max_length=500)]) -> DecisionOut:
        return run(lambda s: s.reject(principal, request_id, reason))

    @server.tool(annotations=HR_DECISION,
                 description="HR only: cancel a pending or approved leave request. The employee assistant may not "
                             "cancel or modify submitted requests (policy 4.8, 12.3).")
    def cancel_leave_request(request_id: Annotated[int, Field(ge=1)],
                             reason: Annotated[str | None, Field(max_length=500)] = None) -> DecisionOut:
        return run(lambda s: s.cancel(principal, request_id, reason))

    return server


def principal_from_args(argv: list[str] | None = None) -> Principal:
    parser = argparse.ArgumentParser(description="Northstar leave MCP server (stdio)")
    parser.add_argument("--role", choices=[r.value for r in Role], default=Role.EMPLOYEE.value)
    parser.add_argument("--employee-id", default=None,
                        help="identity of the caller; default DEMO_EMPLOYEE_ID (local development only)")
    args = parser.parse_args(argv)
    employee_id = (args.employee_id or get_settings().demo_employee_id or "").strip().upper()
    if not employee_id:
        parser.error("no identity: pass --employee-id or set DEMO_EMPLOYEE_ID")
    return Principal(Role(args.role), employee_id)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    principal = principal_from_args(argv)
    try:
        with session_scope() as session:
            verify_principal(session, principal)
    except DomainError as exc:
        print(f"MCP server: {exc.message}", file=sys.stderr)
        return 2
    except Exception as exc:  # never echo connection strings
        print(f"MCP server: database unavailable ({type(exc).__name__})", file=sys.stderr)
        return 2
    build_server(principal).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
