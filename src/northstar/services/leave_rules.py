"""Loads the facts a rule evaluation needs from the database and runs `policies.rules.evaluate`."""

from __future__ import annotations

from sqlalchemy.orm import Session

from northstar.clock import Clock, default_clock
from northstar.database.repositories import (
    EmployeeRepository,
    HolidayRepository,
    LeaveRequestRepository,
    LeaveTypeRepository,
)
from northstar.domain.enums import TYPES_WITHOUT_BALANCE
from northstar.domain.errors import EmployeeNotFound, EntitlementNotFound, UnknownLeaveType
from northstar.policies.rules import (
    EmployeeInfo,
    ExistingRequest,
    LeaveDraft,
    LeaveTypeInfo,
    RuleContext,
    RuleResult,
    evaluate,
)
from northstar.services.balance import BalanceService
from northstar.services.day_calculator import LeaveDayCalculator


class LeaveRuleService:
    def __init__(self, session: Session, clock: Clock | None = None):
        self.session = session
        self.clock = clock or default_clock()
        self.employees = EmployeeRepository(session)
        self.leave_types = LeaveTypeRepository(session)
        self.requests = LeaveRequestRepository(session)
        self.holidays = HolidayRepository(session)

    def calculator(self) -> LeaveDayCalculator:
        return LeaveDayCalculator(self.holidays.dates())

    def build_context(self, employee_id: str, draft: LeaveDraft) -> RuleContext:
        employee = self.employees.get(employee_id)
        if employee is None:
            raise EmployeeNotFound("თანამშრომელი ვერ მოიძებნა.", details={"employee_id": employee_id})
        lt = self.leave_types.get(draft.leave_type)
        if lt is None:
            raise UnknownLeaveType("შვებულების ასეთი სახე არ არსებობს.", details={"leave_type": draft.leave_type})

        available: int | None = None
        if lt.code not in TYPES_WITHOUT_BALANCE:
            try:
                available = BalanceService(self.session).get_balance(
                    employee_id, lt.code, draft.start_date.year).available_days
            except EntitlementNotFound:
                # No entitlement row for that year: nothing can be booked against it.
                available = 0

        active = tuple(
            ExistingRequest(r.request_id, r.leave_type, r.start_date, r.end_date, r.days, r.status)
            for r in self.requests.active_for_employee(employee_id, [t.code for t in self.leave_types.list_all()])
        )
        return RuleContext(
            today=self.clock.today(),
            employee=EmployeeInfo(employee.employee_id, employee.department_code, employee.employment_type,
                                  employee.status, employee.probation_end_date),
            leave_type=LeaveTypeInfo(lt.code, lt.name, lt.day_unit, lt.assistant_supported, lt.self_service),
            calculator=self.calculator(),
            active_requests=active,
            available_days=available,
        )

    def evaluate(self, employee_id: str, draft: LeaveDraft) -> tuple[RuleResult, RuleContext]:
        ctx = self.build_context(employee_id, draft)
        return evaluate(draft, ctx), ctx
