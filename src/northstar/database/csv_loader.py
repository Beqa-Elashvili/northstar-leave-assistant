"""Read and validate the supplied CSV files (read-only; the files are never modified).

The data dictionary specifies ISO dates, but the supplied files use `M/D/YYYY`; both are
accepted. Every row is validated and reported with its file name and line number.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

EXPECTED_HEADERS = {
    "employees.csv": [
        "employee_id", "full_name", "email", "department_code", "department_name", "job_title",
        "employment_type", "start_date", "probation_end_date", "manager_id", "status",
    ],
    "leave_types.csv": [
        "code", "name", "day_unit", "annual_limit_days", "self_service", "assistant_supported", "policy_reference",
    ],
    "leave_entitlements.csv": ["employee_id", "year", "leave_type", "entitled_days", "carried_over_days"],
    "leave_requests.csv": [
        "request_id", "employee_id", "leave_type", "start_date", "end_date", "days", "status",
        "created_at", "created_via", "comment",
    ],
    "public_holidays.csv": ["date", "name"],
}

_US_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_EMPLOYEE_ID = re.compile(r"^E\d{4}$")
REQUEST_STATUSES = {"pending", "approved", "rejected", "cancelled"}
CREATED_VIA = {"portal", "assistant", "hr"}
DAY_UNITS = {"working", "calendar"}


class CsvValidationError(ValueError):
    pass


def parse_date(value: str) -> date:
    """Parse `YYYY-MM-DD` (data dictionary) or `M/D/YYYY` (supplied files)."""
    value = value.strip()
    match = _US_DATE.fullmatch(value)
    if match:
        month, day, year = (int(g) for g in match.groups())
        return date(year, month, day)
    return date.fromisoformat(value)


def parse_bool(value: str) -> bool:
    value = value.strip().lower()
    if value in ("1", "true"):
        return True
    if value in ("0", "false"):
        return False
    raise ValueError(f"expected 1/0, got {value!r}")


def parse_optional_int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value else None


def parse_timestamp(value: str, tz: ZoneInfo) -> datetime:
    parsed = datetime.fromisoformat(value.strip())
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


@dataclass(frozen=True)
class EmployeeRow:
    employee_id: str
    full_name: str
    email: str
    department_code: str
    department_name: str
    job_title: str
    employment_type: str
    start_date: date
    probation_end_date: date
    manager_id: str | None
    status: str


@dataclass(frozen=True)
class LeaveTypeRow:
    code: str
    name: str
    day_unit: str | None
    annual_limit_days: int | None
    self_service: bool
    assistant_supported: bool
    policy_reference: str


@dataclass(frozen=True)
class EntitlementRow:
    employee_id: str
    year: int
    leave_type: str
    entitled_days: int
    carried_over_days: int


@dataclass(frozen=True)
class LeaveRequestRow:
    request_id: int
    employee_id: str
    leave_type: str
    start_date: date
    end_date: date
    days: int
    status: str
    created_at: datetime
    created_via: str
    comment: str | None


@dataclass(frozen=True)
class HolidayRow:
    holiday_date: date
    name: str


@dataclass(frozen=True)
class SeedData:
    employees: list[EmployeeRow]
    leave_types: list[LeaveTypeRow]
    entitlements: list[EntitlementRow]
    requests: list[LeaveRequestRow]
    holidays: list[HolidayRow]


def _read(path: Path) -> list[tuple[int, dict[str, str]]]:
    if not path.is_file():
        raise CsvValidationError(f"{path.name}: file not found")
    # utf-8-sig tolerates a BOM without changing the file.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        expected = EXPECTED_HEADERS[path.name]
        if reader.fieldnames != expected:
            raise CsvValidationError(f"{path.name}: unexpected header {reader.fieldnames}, expected {expected}")
        return [(line, row) for line, row in enumerate(reader, start=2)]


def _rows(path: Path, build):
    result = []
    for line, raw in _read(path):
        try:
            result.append(build({k: (v or "").strip() for k, v in raw.items()}))
        except (ValueError, KeyError) as exc:
            raise CsvValidationError(f"{path.name} line {line}: {exc}") from exc
    return result


def _employee(r: dict[str, str]) -> EmployeeRow:
    for key in ("employee_id", "manager_id"):
        if r[key] and not _EMPLOYEE_ID.fullmatch(r[key]):
            raise ValueError(f"invalid {key} {r[key]!r}")
    for key in ("full_name", "email", "department_code", "department_name", "job_title", "employment_type", "status"):
        if not r[key]:
            raise ValueError(f"{key} is empty")
    return EmployeeRow(
        employee_id=r["employee_id"], full_name=r["full_name"], email=r["email"],
        department_code=r["department_code"], department_name=r["department_name"], job_title=r["job_title"],
        employment_type=r["employment_type"], start_date=parse_date(r["start_date"]),
        probation_end_date=parse_date(r["probation_end_date"]), manager_id=r["manager_id"] or None,
        status=r["status"],
    )


def _leave_type(r: dict[str, str]) -> LeaveTypeRow:
    day_unit = r["day_unit"] or None
    if day_unit is not None and day_unit not in DAY_UNITS:
        raise ValueError(f"invalid day_unit {day_unit!r}")
    return LeaveTypeRow(
        code=r["code"], name=r["name"], day_unit=day_unit,
        annual_limit_days=parse_optional_int(r["annual_limit_days"]),
        self_service=parse_bool(r["self_service"]), assistant_supported=parse_bool(r["assistant_supported"]),
        policy_reference=r["policy_reference"],
    )


def _entitlement(r: dict[str, str]) -> EntitlementRow:
    return EntitlementRow(
        employee_id=r["employee_id"], year=int(r["year"]), leave_type=r["leave_type"],
        entitled_days=int(r["entitled_days"]), carried_over_days=int(r["carried_over_days"] or 0),
    )


def _request(tz: ZoneInfo):
    def build(r: dict[str, str]) -> LeaveRequestRow:
        if r["status"] not in REQUEST_STATUSES:
            raise ValueError(f"invalid status {r['status']!r}")
        if r["created_via"] not in CREATED_VIA:
            raise ValueError(f"invalid created_via {r['created_via']!r}")
        row = LeaveRequestRow(
            request_id=int(r["request_id"]), employee_id=r["employee_id"], leave_type=r["leave_type"],
            start_date=parse_date(r["start_date"]), end_date=parse_date(r["end_date"]), days=int(r["days"]),
            status=r["status"], created_at=parse_timestamp(r["created_at"], tz), created_via=r["created_via"],
            comment=r["comment"] or None,
        )
        if row.end_date < row.start_date:
            raise ValueError("end_date before start_date")
        return row

    return build


def _holiday(r: dict[str, str]) -> HolidayRow:
    if not r["name"]:
        raise ValueError("name is empty")
    return HolidayRow(holiday_date=parse_date(r["date"]), name=r["name"])


def _unique(rows, key, label: str) -> None:
    seen: set = set()
    for row in rows:
        k = key(row)
        if k in seen:
            raise CsvValidationError(f"duplicate {label}: {k}")
        seen.add(k)


def validate_references(data: SeedData) -> None:
    """Cross-file checks performed before anything is written to the database."""
    _unique(data.employees, lambda r: r.employee_id, "employee_id")
    _unique(data.employees, lambda r: r.email.lower(), "email")
    _unique(data.leave_types, lambda r: r.code, "leave type")
    _unique(data.entitlements, lambda r: (r.employee_id, r.year, r.leave_type), "entitlement")
    _unique(data.requests, lambda r: r.request_id, "request_id")
    _unique(data.holidays, lambda r: r.holiday_date, "holiday date")

    employees = {e.employee_id for e in data.employees}
    types = {t.code for t in data.leave_types}
    for e in data.employees:
        if e.manager_id is not None and e.manager_id not in employees:
            raise CsvValidationError(f"employee {e.employee_id}: unknown manager {e.manager_id}")
        if e.manager_id == e.employee_id:
            raise CsvValidationError(f"employee {e.employee_id} is their own manager")
    _check_manager_cycles(data.employees)
    for ent in data.entitlements:
        if ent.employee_id not in employees or ent.leave_type not in types:
            raise CsvValidationError(f"entitlement {ent}: unknown employee or leave type")
        if ent.carried_over_days and ent.leave_type != "ANNUAL":
            raise CsvValidationError(f"entitlement {ent}: carried_over_days applies to ANNUAL only")
    for req in data.requests:
        if req.employee_id not in employees or req.leave_type not in types:
            raise CsvValidationError(f"request {req.request_id}: unknown employee or leave type")


def _check_manager_cycles(employees: list[EmployeeRow]) -> None:
    manager_of = {e.employee_id: e.manager_id for e in employees}
    for start in manager_of:
        seen = {start}
        current = manager_of[start]
        while current is not None:
            if current in seen:
                raise CsvValidationError(f"manager cycle involving {start}")
            seen.add(current)
            current = manager_of.get(current)


def load_seed_data(data_dir: Path, tz: ZoneInfo) -> SeedData:
    data = SeedData(
        employees=_rows(data_dir / "employees.csv", _employee),
        leave_types=_rows(data_dir / "leave_types.csv", _leave_type),
        entitlements=_rows(data_dir / "leave_entitlements.csv", _entitlement),
        requests=_rows(data_dir / "leave_requests.csv", _request(tz)),
        holidays=_rows(data_dir / "public_holidays.csv", _holiday),
    )
    validate_references(data)
    return data
