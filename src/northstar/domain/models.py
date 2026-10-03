"""Typed data transfer objects shared by the services and the MCP tool schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from northstar.services.balance import Balance

__all__ = [
    "Balance", "BalanceList", "EmployeeProfile", "LeaveTypeOut", "LeaveTypeList", "LeaveRequestOut",
    "LeaveRequestList", "ViolationOut", "ProposalOut", "CreateRequestOut", "ProposalStatusOut", "DecisionOut",
]


class EmployeeProfile(BaseModel):
    employee_id: str
    full_name: str
    department_code: str
    department_name: str
    job_title: str
    probation_end_date: date
    role: Literal["employee", "hr"]


class LeaveTypeOut(BaseModel):
    code: str
    name: str
    day_unit: Literal["working", "calendar"] | None
    annual_limit_days: int | None
    self_service: bool = Field(description="Can be submitted through the HR portal")
    assistant_supported: bool = Field(description="The HR assistant may create this request type")
    policy_reference: str


class LeaveTypeList(BaseModel):
    leave_types: list[LeaveTypeOut]


class BalanceList(BaseModel):
    employee_id: str
    year: int
    balances: list[Balance]


class LeaveRequestOut(BaseModel):
    request_id: int
    employee_id: str
    leave_type: str
    start_date: date
    end_date: date
    days: int
    status: Literal["pending", "approved", "rejected", "cancelled"]
    created_at: datetime
    created_via: Literal["portal", "assistant", "hr"]
    comment: str | None
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None


class LeaveRequestList(BaseModel):
    requests: list[LeaveRequestOut]
    count: int


class ViolationOut(BaseModel):
    code: str
    kind: Literal["rejected", "redirect", "needs_input"]
    message: str = Field(description="User-facing explanation in Georgian")
    article: str = Field(description="Leave and Absence Policy article")
    redirect_to: Literal["hr", "hr_portal", "manager"] | None = None
    details: dict = Field(default_factory=dict)


class ProposalOut(BaseModel):
    outcome: Literal["awaiting_confirmation", "needs_input", "not_allowed"]
    proposal_id: uuid.UUID | None = Field(None, description="Present only when awaiting confirmation")
    conversation_id: uuid.UUID
    leave_type: str
    leave_type_name: str
    start_date: date
    end_date: date
    days: int | None
    day_unit: Literal["working", "calendar"] | None
    comment: str | None
    available_days_before: int | None = Field(None, description="Available balance before this request")
    available_days_after: int | None = None
    expires_at: datetime | None = None
    violations: list[ViolationOut] = Field(default_factory=list)


class CreateRequestOut(BaseModel):
    request: LeaveRequestOut
    proposal_id: uuid.UUID
    already_existed: bool = Field(description="True when the proposal had already been confirmed (idempotent replay)")


class ProposalStatusOut(BaseModel):
    proposal_id: uuid.UUID
    status: Literal["proposed", "confirmed", "declined", "expired"]


class DecisionOut(BaseModel):
    request: LeaveRequestOut
    previous_status: str
