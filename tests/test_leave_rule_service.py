"""Rules evaluated against the seeded database (real employees, balances and requests)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from northstar.clock import FrozenClock
from northstar.domain.errors import EmployeeNotFound, UnknownLeaveType
from northstar.policies.rules import LeaveDraft
from northstar.services.leave_rules import LeaveRuleService

pytestmark = pytest.mark.db
D = date
CLOCK = FrozenClock(datetime(2026, 10, 19, 11, 0, tzinfo=ZoneInfo("Asia/Tbilisi")))


@pytest.fixture()
def svc(session):
    return LeaveRuleService(session, CLOCK)


def run(svc, employee, lt, start, end, **kw):
    result, _ = svc.evaluate(employee, LeaveDraft(lt, start, end, **kw))
    return result, [v.code for v in result.violations]


def test_scenario_2_annual_26_to_30_october_violates_notice(svc):
    r, codes = run(svc, "E1001", "ANNUAL", D(2026, 10, 26), D(2026, 10, 30))
    assert codes == ["notice_period"] and r.days == 5


def test_annual_27_to_30_october_passes_for_nino(svc):
    r, codes = run(svc, "E1001", "ANNUAL", D(2026, 10, 27), D(2026, 10, 30))
    assert r.ok and r.days == 4


def test_overlap_with_seeded_pending_request(svc):
    _, codes = run(svc, "E1001", "ANNUAL", D(2026, 11, 11), D(2026, 11, 13))
    assert "overlapping_request" in codes                      # request #5 (pending)


def test_rejected_request_does_not_block(svc):
    # E1002's #8 for 26–30 Oct was rejected; dates are free again (still subject to notice).
    _, codes = run(svc, "E1002", "ANNUAL", D(2026, 10, 27), D(2026, 10, 28))
    assert "overlapping_request" not in codes


def test_balance_from_database(svc):
    _, codes = run(svc, "E1002", "ANNUAL", D(2026, 11, 2), D(2026, 11, 6))   # 5 days, 4 available
    assert codes == ["insufficient_balance"]


def test_probation_employee(svc):
    _, codes = run(svc, "E1004", "ANNUAL", D(2026, 11, 24), D(2026, 11, 26))
    assert codes == ["probation"]
    r, codes = run(svc, "E1004", "ANNUAL", D(2026, 12, 1), D(2026, 12, 4))
    assert r.ok


def test_audit_employee_december(svc):
    _, codes = run(svc, "E1005", "ANNUAL", D(2026, 12, 7), D(2026, 12, 9))
    assert codes == ["restricted_period"]
    r, _ = run(svc, "E1003", "ANNUAL", D(2026, 12, 7), D(2026, 12, 9))       # TAX: allowed
    assert r.ok


def test_sick_over_remaining_paid_days(svc):
    r, codes = run(svc, "E1001", "SICK", D(2026, 10, 19), D(2026, 10, 29), sick_period_known_in_advance=True)
    assert r.days == 9 and codes == ["sick_paid_limit_exceeded"]             # 8 paid days left


def test_unpaid_valid(svc):
    r, codes = run(svc, "E1001", "UNPAID", D(2026, 11, 3), D(2026, 11, 6), comment="ოჯახური მიზეზი")
    assert r.ok and r.days == 4


@pytest.mark.parametrize("lt", ["BEREAVEMENT", "STUDY", "PARENTAL"])
def test_unsupported_types(svc, lt):
    _, codes = run(svc, "E1009", lt, D(2026, 11, 16), D(2026, 11, 16))
    assert codes == ["assistant_unsupported_type"]


def test_unknown_employee_and_type(svc):
    with pytest.raises(EmployeeNotFound):
        run(svc, "E9999", "ANNUAL", D(2026, 11, 2), D(2026, 11, 2))
    with pytest.raises(UnknownLeaveType):
        run(svc, "E1001", "VACATION", D(2026, 11, 2), D(2026, 11, 2))
