"""Server-side authorization for the MCP tools.

Identity is never taken from tool arguments. The MCP server process is bound to one
`Principal` when it starts (local development mechanism, see README):

- `employee`: the employee assistant. Sees and acts only on the caller's own data
  (policy 5.2, 12.3) and can never approve, reject, cancel or modify requests (4.8, 12.3).
- `hr`: an HR user. Must be an active employee of the HR department (HRS, policy 13.3);
  may read any employee's data and approve, reject or cancel requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from northstar.database.repositories import EmployeeRepository
from northstar.domain.errors import AuthenticationError, PermissionDenied

HR_DEPARTMENT = "HRS"


class Role(StrEnum):
    EMPLOYEE = "employee"
    HR = "hr"


@dataclass(frozen=True)
class Principal:
    role: Role
    employee_id: str

    @property
    def is_hr(self) -> bool:
        return self.role == Role.HR


def verify_principal(session: Session, principal: Principal) -> None:
    """Re-checked on every tool call so a deactivated identity loses access immediately."""
    employee = EmployeeRepository(session).get(principal.employee_id)
    if employee is None or employee.status != "active":
        raise AuthenticationError("მომხმარებლის იდენტიფიკაცია ვერ მოხერხდა.")
    if principal.role == Role.HR and employee.department_code != HR_DEPARTMENT:
        raise AuthenticationError("HR როლი მხოლოდ ადამიანური რესურსების სამსახურის აქტიურ თანამშრომელს ეკუთვნის.")


def resolve_target_employee(principal: Principal, requested_employee_id: str | None) -> str:
    """Which employee's data a read operation may target."""
    if requested_employee_id is None:
        return principal.employee_id
    target = requested_employee_id.strip().upper()
    if principal.is_hr or target == principal.employee_id:
        return target
    raise PermissionDenied(
        "სხვა თანამშრომლის მონაცემების ნახვა შეუძლებელია: ასისტენტი მხოლოდ თქვენს საკუთარ ბალანსსა და "
        "მოთხოვნებს აჩვენებს, მათ შორის ხელმძღვანელისთვისაც. გუნდის მონაცემებს ხელმძღვანელი HR პორტალზე ნახულობს.",
        article="5.2, 12.3",
    )


def require_hr(principal: Principal, action: str) -> None:
    if principal.is_hr:
        return
    raise PermissionDenied(
        f"HR ასისტენტს არ შეუძლია უკვე წარდგენილი მოთხოვნის {action}. მოთხოვნის გაუქმება ან შეცვლა HR პორტალით "
        "შეგიძლიათ; დამტკიცებასა და უარყოფაზე გადაწყვეტილებას უშუალო ხელმძღვანელი იღებს.",
        article="4.8, 12.3",
        details={"action": action},
    )
