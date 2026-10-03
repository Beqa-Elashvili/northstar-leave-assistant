"""Leave balance (Leave and Absence Policy 5.1, data dictionary formula).

available = entitled_days + carried_over_days - approved_days - pending_days

Only requests of the same employee, leave type and leave year (year of the first day) are
counted; rejected and cancelled requests are ignored. For SICK, a negative result is shown
as 0 available *paid* days; this never means further sickness cannot be recorded (6.4).
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from northstar.database.repositories import (
    EmployeeRepository,
    EntitlementRepository,
    LeaveRequestRepository,
    LeaveTypeRepository,
)
from northstar.domain.enums import TYPES_WITHOUT_BALANCE, LeaveTypeCode
from northstar.domain.errors import (
    EmployeeNotFound,
    EntitlementNotFound,
    NoBalanceForLeaveType,
    UnknownLeaveType,
)


class Balance(BaseModel):
    employee_id: str
    leave_type: str
    year: int
    entitled_days: int
    carried_over_days: int
    approved_days: int
    pending_days: int
    available_days: int = Field(description="Days that can still be requested (SICK: paid days, never below 0)")
    calculated_available_days: int = Field(description="Raw formula result before the SICK floor at 0")
    unit: str | None = Field(description="working or calendar")


def compute_available(leave_type: str, entitled: int, carried_over: int, approved: int, pending: int) -> tuple[int, int]:
    """Return (available_days, calculated_available_days)."""
    calculated = entitled + carried_over - approved - pending
    if leave_type == LeaveTypeCode.SICK and calculated < 0:
        return 0, calculated
    return calculated, calculated


class BalanceService:
    def __init__(self, session: Session):
        self.employees = EmployeeRepository(session)
        self.leave_types = LeaveTypeRepository(session)
        self.entitlements = EntitlementRepository(session)
        self.requests = LeaveRequestRepository(session)

    def get_balance(self, employee_id: str, leave_type: str, year: int) -> Balance:
        if self.employees.get(employee_id) is None:
            raise EmployeeNotFound("თანამშრომელი ვერ მოიძებნა.", details={"employee_id": employee_id})
        lt = self.leave_types.get(leave_type)
        if lt is None:
            raise UnknownLeaveType("შვებულების ასეთი სახე არ არსებობს.", details={"leave_type": leave_type})
        if leave_type in TYPES_WITHOUT_BALANCE:
            raise NoBalanceForLeaveType(
                f"{lt.name}-ს წლიური ბალანსი არ აქვს.", article="5.1", details={"leave_type": leave_type}
            )
        entitlement = self.entitlements.get(employee_id, year, leave_type)
        if entitlement is None:
            raise EntitlementNotFound(
                f"{year} წლისთვის ამ სახის კუთვნილი დღეების მონაცემი HR სისტემაში არ არის.",
                details={"leave_type": leave_type, "year": year},
            )
        used = self.requests.used_days(employee_id, leave_type, year)
        available, calculated = compute_available(
            leave_type, entitlement.entitled_days, entitlement.carried_over_days, used.approved, used.pending
        )
        return Balance(
            employee_id=employee_id,
            leave_type=leave_type,
            year=year,
            entitled_days=entitlement.entitled_days,
            carried_over_days=entitlement.carried_over_days,
            approved_days=used.approved,
            pending_days=used.pending,
            available_days=available,
            calculated_available_days=calculated,
            unit=lt.day_unit,
        )

    def get_all_balances(self, employee_id: str, year: int) -> list[Balance]:
        """Balances for every leave type that has one (types without an entitlement row are skipped)."""
        return [self.get_balance(employee_id, e.leave_type, year)
                for e in self.entitlements.list_for_employee(employee_id, year)]
