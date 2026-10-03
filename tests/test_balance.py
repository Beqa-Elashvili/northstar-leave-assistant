"""BalanceService: policy 5.1 formula on the seeded data and on crafted scenarios."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from northstar.database.models import LeaveRequest
from northstar.domain.errors import EmployeeNotFound, EntitlementNotFound, NoBalanceForLeaveType, UnknownLeaveType
from northstar.services.balance import BalanceService, compute_available

TZ = ZoneInfo("Asia/Tbilisi")
NOW = datetime(2026, 10, 19, 10, tzinfo=TZ)


# --- pure formula --------------------------------------------------------------------------

def test_formula():
    assert compute_available("ANNUAL", 25, 3, 15, 3) == (10, 10)


def test_annual_can_be_negative_in_raw_data():
    assert compute_available("ANNUAL", 10, 0, 12, 0) == (-2, -2)


def test_sick_negative_is_floored_at_zero():
    assert compute_available("SICK", 10, 0, 11, 2) == (0, -3)


# --- seeded data -----------------------------------------------------------------------------

pytestmark_db = pytest.mark.db


def add(session, employee, lt, start, end, days, status):
    session.add(LeaveRequest(employee_id=employee, leave_type=lt, start_date=start, end_date=end, days=days,
                             status=status, created_at=NOW, created_via="portal"))
    session.flush()


@pytestmark_db
def test_seeded_annual_balance_with_carry_over(session):
    b = BalanceService(session).get_balance("E1001", "ANNUAL", 2026)
    assert (b.entitled_days, b.carried_over_days, b.approved_days, b.pending_days, b.available_days) == (25, 3, 15, 3, 10)
    assert b.unit == "working"


@pytestmark_db
def test_rejected_request_not_counted(session):
    b = BalanceService(session).get_balance("E1002", "ANNUAL", 2026)
    assert (b.approved_days, b.pending_days, b.available_days) == (20, 0, 4)


@pytestmark_db
def test_cancelled_request_not_counted(session):
    # E1001 #3 (2 days) is cancelled; approved stays 5 + 10.
    assert BalanceService(session).get_balance("E1001", "ANNUAL", 2026).approved_days == 15


@pytestmark_db
def test_pending_reduces_available(session):
    svc = BalanceService(session)
    before = svc.get_balance("E1003", "ANNUAL", 2026)
    add(session, "E1003", "ANNUAL", date(2026, 12, 7), date(2026, 12, 9), 3, "pending")
    after = svc.get_balance("E1003", "ANNUAL", 2026)
    assert after.pending_days == before.pending_days + 3
    assert after.available_days == before.available_days - 3


@pytestmark_db
def test_approved_reduces_available(session):
    svc = BalanceService(session)
    add(session, "E1003", "ANNUAL", date(2026, 12, 14), date(2026, 12, 15), 2, "approved")
    assert svc.get_balance("E1003", "ANNUAL", 2026).approved_days == 11 + 2


@pytestmark_db
def test_other_leave_type_and_year_not_counted(session):
    svc = BalanceService(session)
    add(session, "E1003", "SICK", date(2026, 11, 2), date(2026, 11, 3), 2, "approved")
    add(session, "E1003", "ANNUAL", date(2025, 12, 1), date(2025, 12, 5), 5, "approved")
    b = svc.get_balance("E1003", "ANNUAL", 2026)
    assert (b.approved_days, b.available_days) == (11, 15)


@pytestmark_db
def test_sick_balance_and_negative_handling(session):
    svc = BalanceService(session)
    assert svc.get_balance("E1001", "SICK", 2026).available_days == 8
    add(session, "E1001", "SICK", date(2026, 9, 1), date(2026, 9, 11), 9, "approved")
    b = svc.get_balance("E1001", "SICK", 2026)
    assert (b.approved_days, b.calculated_available_days, b.available_days) == (11, -1, 0)


@pytestmark_db
def test_study_and_unpaid_balances(session):
    svc = BalanceService(session)
    assert svc.get_balance("E1009", "STUDY", 2026).available_days == 4
    unpaid = svc.get_balance("E1001", "UNPAID", 2026)
    assert (unpaid.available_days, unpaid.unit) == (30, "calendar")


@pytestmark_db
def test_all_balances(session):
    balances = BalanceService(session).get_all_balances("E1001", 2026)
    assert {b.leave_type: b.available_days for b in balances} == {"ANNUAL": 10, "SICK": 8, "STUDY": 5, "UNPAID": 30}


@pytestmark_db
def test_matches_database_view_for_everyone(session):
    svc = BalanceService(session)
    rows = session.execute(text("SELECT employee_id, leave_type, year, available_days, approved_days, pending_days"
                                " FROM leave_balances")).all()
    assert len(rows) == 56
    for r in rows:
        b = svc.get_balance(r.employee_id, r.leave_type, r.year)
        assert (b.available_days, b.approved_days, b.pending_days) == (r.available_days, r.approved_days, r.pending_days)


@pytestmark_db
@pytest.mark.parametrize("args, error", [
    (("E9999", "ANNUAL", 2026), EmployeeNotFound),
    (("E1001", "HOLIDAY", 2026), UnknownLeaveType),
    (("E1001", "BEREAVEMENT", 2026), NoBalanceForLeaveType),
    (("E1001", "PARENTAL", 2026), NoBalanceForLeaveType),
    (("E1001", "ANNUAL", 2027), EntitlementNotFound),
])
def test_errors(session, args, error):
    with pytest.raises(error) as exc:
        BalanceService(session).get_balance(*args)
    assert exc.value.message  # Georgian, user-safe text
