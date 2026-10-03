"""Repositories: the only place that builds SQL for the HR tables.

All queries use SQLAlchemy expressions with bound parameters (no string-built SQL).
Repositories hold no business rules; validation lives in `northstar.services` / `northstar.policies`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from northstar.database.models import (
    Employee,
    LeaveEntitlement,
    LeaveProposal,
    LeaveRequest,
    LeaveType,
    PublicHoliday,
)

ACTIVE_STATUSES = ("approved", "pending")


class EmployeeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, employee_id: str) -> Employee | None:
        return self.session.get(Employee, employee_id)

    def list_all(self) -> list[Employee]:
        return list(self.session.scalars(select(Employee).order_by(Employee.employee_id)))


class LeaveTypeRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, code: str) -> LeaveType | None:
        return self.session.get(LeaveType, code)

    def list_all(self) -> list[LeaveType]:
        return list(self.session.scalars(select(LeaveType).order_by(LeaveType.code)))


class EntitlementRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, employee_id: str, year: int, leave_type: str) -> LeaveEntitlement | None:
        return self.session.get(LeaveEntitlement, (employee_id, year, leave_type))

    def list_for_employee(self, employee_id: str, year: int) -> list[LeaveEntitlement]:
        stmt = (
            select(LeaveEntitlement)
            .where(LeaveEntitlement.employee_id == employee_id, LeaveEntitlement.year == year)
            .order_by(LeaveEntitlement.leave_type)
        )
        return list(self.session.scalars(stmt))


class HolidayRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_all(self) -> list[PublicHoliday]:
        return list(self.session.scalars(select(PublicHoliday).order_by(PublicHoliday.holiday_date)))

    def dates(self) -> frozenset[date]:
        return frozenset(self.session.scalars(select(PublicHoliday.holiday_date)))


@dataclass(frozen=True)
class RequestFilter:
    employee_id: str | None = None
    statuses: tuple[str, ...] | None = None
    leave_type: str | None = None
    # Requests whose period overlaps [date_from, date_to] (either bound optional).
    date_from: date | None = None
    date_to: date | None = None
    limit: int = 200


@dataclass(frozen=True)
class UsedDays:
    approved: int
    pending: int


class LeaveRequestRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, request_id: int, *, for_update: bool = False) -> LeaveRequest | None:
        stmt = select(LeaveRequest).where(LeaveRequest.request_id == request_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalars(stmt).first()

    def list(self, flt: RequestFilter) -> list[LeaveRequest]:
        stmt = select(LeaveRequest)
        if flt.employee_id is not None:
            stmt = stmt.where(LeaveRequest.employee_id == flt.employee_id)
        if flt.statuses:
            stmt = stmt.where(LeaveRequest.status.in_(flt.statuses))
        if flt.leave_type is not None:
            stmt = stmt.where(LeaveRequest.leave_type == flt.leave_type)
        if flt.date_from is not None:
            stmt = stmt.where(LeaveRequest.end_date >= flt.date_from)
        if flt.date_to is not None:
            stmt = stmt.where(LeaveRequest.start_date <= flt.date_to)
        stmt = stmt.order_by(LeaveRequest.start_date, LeaveRequest.request_id).limit(flt.limit)
        return list(self.session.scalars(stmt))

    def used_days(self, employee_id: str, leave_type: str, year: int) -> UsedDays:
        """Approved and pending days of one employee/type in one leave year (policy 2.1: year of the first day)."""
        stmt = (
            select(LeaveRequest.status, func.coalesce(func.sum(LeaveRequest.days), 0))
            .where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.leave_type == leave_type,
                extract("year", LeaveRequest.start_date) == year,
                LeaveRequest.status.in_(ACTIVE_STATUSES),
            )
            .group_by(LeaveRequest.status)
        )
        totals = {status: int(total) for status, total in self.session.execute(stmt)}
        return UsedDays(approved=totals.get("approved", 0), pending=totals.get("pending", 0))

    def active_overlapping(self, employee_id: str, start: date, end: date) -> list[LeaveRequest]:
        """Approved/pending requests of the employee (any type) that share at least one day with [start, end]."""
        return self.list(RequestFilter(employee_id=employee_id, statuses=ACTIVE_STATUSES, date_from=start, date_to=end))

    def active_for_employee(self, employee_id: str, leave_types: Iterable[str]) -> list[LeaveRequest]:
        stmt = (
            select(LeaveRequest)
            .where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.status.in_(ACTIVE_STATUSES),
                LeaveRequest.leave_type.in_(tuple(leave_types)),
            )
            .order_by(LeaveRequest.start_date)
        )
        return list(self.session.scalars(stmt))

    def add(self, request: LeaveRequest) -> LeaveRequest:
        self.session.add(request)
        self.session.flush()  # assigns request_id
        return request


class ProposalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, proposal_id: uuid.UUID, *, for_update: bool = False) -> LeaveProposal | None:
        stmt = select(LeaveProposal).where(LeaveProposal.proposal_id == proposal_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalars(stmt).first()

    def add(self, proposal: LeaveProposal) -> LeaveProposal:
        self.session.add(proposal)
        self.session.flush()
        return proposal

    def open_for_conversation(self, employee_id: str, conversation_id: uuid.UUID) -> list[LeaveProposal]:
        stmt = select(LeaveProposal).where(
            LeaveProposal.employee_id == employee_id,
            LeaveProposal.conversation_id == conversation_id,
            LeaveProposal.status == "proposed",
        )
        return list(self.session.scalars(stmt))

    def expire_before(self, moment: datetime) -> int:
        stale = self.session.scalars(
            select(LeaveProposal).where(LeaveProposal.status == "proposed", LeaveProposal.expires_at <= moment)
        ).all()
        for proposal in stale:
            proposal.status = "expired"
            proposal.resolved_at = moment
        return len(stale)
