"""Policy rules (pure, no database). APP_TODAY = 2026-10-19 (Monday)."""

from dataclasses import replace
from datetime import date

import pytest

from northstar.policies.rules import (
    EmployeeInfo, ExistingRequest, LeaveDraft, LeaveTypeInfo, RuleContext, _restricted_days, evaluate,
)
from northstar.services.day_calculator import LeaveDayCalculator

D = date
TODAY = D(2026, 10, 19)

TYPES = {
    "ANNUAL": LeaveTypeInfo("ANNUAL", "ყოველწლიური ანაზღაურებადი შვებულება", "working", True, True),
    "SICK": LeaveTypeInfo("SICK", "ავადმყოფობის შვებულება", "working", True, True),
    "UNPAID": LeaveTypeInfo("UNPAID", "უხელფასო შვებულება", "calendar", True, True),
    "BEREAVEMENT": LeaveTypeInfo("BEREAVEMENT", "გლოვის შვებულება", "working", False, True),
    "STUDY": LeaveTypeInfo("STUDY", "სასწავლო და საგამოცდო შვებულება", "working", False, True),
    "PARENTAL": LeaveTypeInfo("PARENTAL", "მშობლის შვებულება", None, False, False),
}


@pytest.fixture(scope="module")
def calc(seed_data):
    return LeaveDayCalculator(h.holiday_date for h in seed_data.holidays)


@pytest.fixture()
def check(calc):
    def run(leave_type, start, end, *, comment=None, known=False, dept="TAX", probation=D(2019, 6, 3),
            available=10, existing=(), employment="full_time", status="active"):
        ctx = RuleContext(
            today=TODAY,
            employee=EmployeeInfo("E0001", dept, employment, status, probation),
            leave_type=TYPES[leave_type],
            calculator=calc,
            active_requests=tuple(existing),
            available_days=available,
        )
        return evaluate(LeaveDraft(leave_type, start, end, comment, known), ctx)
    return run


def codes(result):
    return [v.code for v in result.violations]


def req(rid, start, end, days, lt="ANNUAL", status="approved"):
    return ExistingRequest(rid, lt, start, end, days, status)


# --- assistant-supported types -----------------------------------------------------------------

@pytest.mark.parametrize("lt, channel, article", [
    ("BEREAVEMENT", "hr_portal", "8.1–8.3"), ("STUDY", "hr_portal", "9.1–9.3"), ("PARENTAL", "hr", "10.1–10.2"),
])
def test_unsupported_types_redirect_without_creating(check, lt, channel, article):
    r = check(lt, D(2026, 11, 2), D(2026, 11, 3))
    assert not r.ok and r.days is None
    (v,) = r.violations
    assert (v.code, v.kind, v.redirect_to, v.article) == ("assistant_unsupported_type", "redirect", channel, article)


def test_study_explains_approved_plan_requirement(check):
    msg = check("STUDY", D(2026, 11, 16), D(2026, 11, 16)).violations[0].message
    assert "5 სამუშაო დღე" in msg and "დამტკიცებულ" in msg and "HR ამოწმებს" in msg


def test_bereavement_explains_days(check):
    msg = check("BEREAVEMENT", D(2026, 10, 20), D(2026, 10, 20)).violations[0].message
    assert "3 სამუშაო დღე" in msg and "1 სამუშაო დღე" in msg and "HR პორტალით" in msg


# --- structural ---------------------------------------------------------------------------------

def test_reversed_dates(check):
    assert codes(check("ANNUAL", D(2026, 11, 5), D(2026, 11, 2))) == ["invalid_date_range"]


def test_cross_year_rejected(check):
    assert codes(check("ANNUAL", D(2026, 12, 28), D(2027, 1, 8))) == ["crosses_leave_year"]


def test_next_year_redirects_to_portal(check):
    r = check("ANNUAL", D(2027, 2, 1), D(2027, 2, 2))
    assert codes(r) == ["not_current_year"] and r.violations[0].redirect_to == "hr_portal"
    assert "1 დეკემბრიდან" in r.violations[0].message


def test_past_year_rejected(check):
    assert codes(check("SICK", D(2025, 12, 29), D(2025, 12, 30))) == ["not_current_year"]


def test_weekend_only_period(check):
    assert codes(check("ANNUAL", D(2026, 10, 31), D(2026, 11, 1))) == ["no_working_days"]


def test_not_full_time_or_inactive(check):
    assert codes(check("ANNUAL", D(2026, 11, 10), D(2026, 11, 10), employment="part_time")) == ["not_full_time"]
    assert codes(check("ANNUAL", D(2026, 11, 10), D(2026, 11, 10), status="terminated")) == ["employee_not_active"]


def test_overlap_with_own_active_request(check):
    r = check("ANNUAL", D(2026, 11, 11), D(2026, 11, 13), existing=[req(5, D(2026, 11, 9), D(2026, 11, 11), 3, status="pending")])
    assert "overlapping_request" in codes(r)
    assert r.violations[0].details["request_ids"] == [5]


def test_overlap_applies_across_types(check):
    r = check("SICK", D(2026, 10, 19), D(2026, 10, 19), existing=[req(9, D(2026, 10, 19), D(2026, 10, 20), 2)])
    assert "overlapping_request" in codes(r)


# --- ANNUAL --------------------------------------------------------------------------------------

def test_annual_ok(check):
    r = check("ANNUAL", D(2026, 10, 27), D(2026, 10, 30))
    assert r.ok and r.days == 4


def test_annual_notice_5_days_fails_for_26_october(check):
    r = check("ANNUAL", D(2026, 10, 26), D(2026, 10, 30))
    assert codes(r) == ["notice_period"]
    v = r.violations[0]
    assert v.article == "4.4" and v.details == {"required_working_days": 5, "actual_working_days": 4,
                                                "earliest_start_date": "2026-10-27"}
    assert "ხელმძღვანელ" in v.message  # emergency route is the manager, not the assistant


def test_annual_notice_15_days_for_6_or_more(check):
    r = check("ANNUAL", D(2026, 11, 2), D(2026, 11, 9))  # 6 working days, notice 10
    assert codes(r) == ["notice_period"] and r.violations[0].details["earliest_start_date"] == "2026-11-10"
    assert check("ANNUAL", D(2026, 11, 10), D(2026, 11, 17)).ok


def test_annual_15_days_allowed(check):
    r = check("ANNUAL", D(2026, 11, 10), D(2026, 12, 1), available=20)
    assert r.days == 15 and r.ok


def test_annual_16_days_rejected(check):
    r = check("ANNUAL", D(2026, 11, 10), D(2026, 12, 2), available=20)
    assert r.days == 16 and codes(r) == ["annual_max_length"]
    assert r.violations[0].redirect_to == "manager" and r.violations[0].article == "4.5"


def test_annual_chain_over_weekend_and_holiday_exceeds_15(check):
    existing = [req(40, D(2026, 11, 11), D(2026, 11, 20), 8)]
    assert check("ANNUAL", D(2026, 11, 24), D(2026, 12, 1), existing=existing, available=20).ok   # 8 + 6
    r = check("ANNUAL", D(2026, 11, 24), D(2026, 12, 3), existing=existing, available=20)          # 8 + 8
    assert codes(r) == ["annual_max_length"]
    assert r.violations[0].details == {"total_working_days": 16, "chained_request_ids": [40]}


def test_annual_chain_is_transitive(check):
    existing = [req(41, D(2026, 10, 30), D(2026, 11, 6), 6), req(42, D(2026, 11, 16), D(2026, 11, 20), 5)]
    r = check("ANNUAL", D(2026, 11, 9), D(2026, 11, 13), existing=existing, available=20)
    assert codes(r) == ["annual_max_length"]
    assert r.violations[0].details["chained_request_ids"] == [41, 42]


def test_annual_working_day_gap_breaks_chain(check):
    existing = [req(43, D(2026, 11, 2), D(2026, 11, 6), 5)]
    assert check("ANNUAL", D(2026, 11, 10), D(2026, 11, 20), existing=existing, available=20).ok  # Mon 11-09 between


def test_annual_chain_ignores_other_leave_types(check):
    existing = [req(44, D(2026, 11, 2), D(2026, 11, 6), 5, lt="SICK")]
    assert check("ANNUAL", D(2026, 11, 9), D(2026, 11, 27), available=30, existing=existing).days == 14


def test_probation_blocks_annual(check):
    r = check("ANNUAL", D(2026, 11, 24), D(2026, 11, 27), probation=D(2026, 11, 30), dept="TEC")
    assert codes(r) == ["probation"]
    v = r.violations[0]
    assert v.redirect_to == "hr" and v.details["earliest_start_date"] == "2026-12-01"


def test_probation_last_day_is_inclusive(check):
    assert "probation" in codes(check("ANNUAL", D(2026, 11, 30), D(2026, 11, 30), probation=D(2026, 11, 30)))
    assert check("ANNUAL", D(2026, 12, 1), D(2026, 12, 4), probation=D(2026, 11, 30), dept="TEC").ok


@pytest.mark.parametrize("lt, start, end", [
    ("SICK", D(2026, 10, 19), D(2026, 10, 19)),
    ("UNPAID", D(2026, 11, 3), D(2026, 11, 5)),
])
def test_probation_does_not_apply_to_sick_and_unpaid(check, lt, start, end):
    r = check(lt, start, end, probation=D(2026, 11, 30), comment="პირადი მიზეზი", available=30)
    assert "probation" not in codes(r) and r.ok


def test_audit_restricted_december(check):
    r = check("ANNUAL", D(2026, 12, 14), D(2026, 12, 18), dept="AUD")
    assert codes(r) == ["restricted_period"]
    v = r.violations[0]
    assert v.article == "4.6" and v.redirect_to == "manager"


def test_audit_restriction_any_single_day(check):
    assert "restricted_period" in codes(check("ANNUAL", D(2026, 11, 30), D(2026, 12, 1), dept="AUD"))
    assert check("ANNUAL", D(2026, 12, 21), D(2026, 12, 25), dept="AUD").ok   # after 20 December


def test_restriction_only_for_audit(check):
    assert check("ANNUAL", D(2026, 12, 14), D(2026, 12, 18), dept="TAX").ok


def test_restricted_period_boundaries():
    assert _restricted_days(D(2027, 1, 14), D(2027, 1, 15)) == [D(2027, 1, 15)]
    assert _restricted_days(D(2027, 3, 15), D(2027, 3, 16)) == [D(2027, 3, 15)]
    assert _restricted_days(D(2026, 12, 20), D(2026, 12, 21)) == [D(2026, 12, 20)]
    assert _restricted_days(D(2026, 11, 30), D(2026, 11, 30)) == []


def test_annual_insufficient_balance(check):
    r = check("ANNUAL", D(2026, 10, 27), D(2026, 10, 30), available=3)
    assert codes(r) == ["insufficient_balance"] and r.violations[0].article == "5.1"


def test_multiple_violations_reported_together(check):
    r = check("ANNUAL", D(2026, 12, 1), D(2026, 12, 2), dept="AUD", available=1, probation=D(2026, 12, 31))
    assert set(codes(r)) == {"probation", "restricted_period", "insufficient_balance"}


# --- SICK ----------------------------------------------------------------------------------------

def test_sick_today_ok(check):
    r = check("SICK", D(2026, 10, 19), D(2026, 10, 20), available=8)
    assert r.ok and r.days == 2


def test_sick_late_submission_window(check):
    assert check("SICK", D(2026, 10, 15), D(2026, 10, 16)).ok                      # Fri, Mon after = 2
    assert codes(check("SICK", D(2026, 10, 14), D(2026, 10, 16))) == ["sick_late_submission"]


def test_sick_future_needs_confirmation(check):
    r = check("SICK", D(2026, 11, 2), D(2026, 11, 4))
    assert codes(r) == ["sick_future_confirmation"] and r.violations[0].kind == "needs_input"
    assert check("SICK", D(2026, 11, 2), D(2026, 11, 4), known=True).ok


def test_sick_over_paid_balance_redirects_to_hr(check):
    r = check("SICK", D(2026, 10, 19), D(2026, 10, 21), available=2)
    assert codes(r) == ["sick_paid_limit_exceeded"]
    v = r.violations[0]
    assert (v.kind, v.redirect_to, v.article) == ("redirect", "hr", "6.4")
    assert "არ ნიშნავს" in v.message  # never claims sickness can no longer be recorded


def test_sick_zero_balance_redirects(check):
    assert codes(check("SICK", D(2026, 10, 19), D(2026, 10, 19), available=0)) == ["sick_paid_limit_exceeded"]


def test_sick_has_no_notice_rule(check):
    assert check("SICK", D(2026, 10, 20), D(2026, 10, 20), known=True).ok


# --- UNPAID --------------------------------------------------------------------------------------

def test_unpaid_ok_counts_calendar_days(check):
    r = check("UNPAID", D(2026, 11, 6), D(2026, 11, 9), comment="ოჯახური მიზეზი", available=30)
    assert r.ok and r.days == 4  # Fri–Mon including the weekend


def test_unpaid_includes_holidays(check):
    assert check("UNPAID", D(2026, 11, 20), D(2026, 11, 29), comment="პირადი", available=30).days == 10


def test_unpaid_requires_reason(check):
    r = check("UNPAID", D(2026, 11, 3), D(2026, 11, 5), comment="  ", available=30)
    assert codes(r) == ["reason_required"] and r.violations[0].kind == "needs_input"


def test_unpaid_rejects_health_details(check):
    assert codes(check("UNPAID", D(2026, 11, 3), D(2026, 11, 5), comment="ექიმთან ოპერაცია მაქვს", available=30)) \
        == ["health_details_in_reason"]


def test_unpaid_notice_10_working_days(check):
    r = check("UNPAID", D(2026, 11, 2), D(2026, 11, 2), comment="პირადი", available=30)
    assert codes(r) == ["notice_period"]
    assert r.violations[0].article == "7.2" and r.violations[0].details["earliest_start_date"] == "2026-11-03"
    assert check("UNPAID", D(2026, 11, 3), D(2026, 11, 3), comment="პირადი", available=30).ok


def test_unpaid_30_day_limit(check):
    assert check("UNPAID", D(2026, 11, 3), D(2026, 12, 2), comment="პირადი", available=30).ok      # exactly 30
    r = check("UNPAID", D(2026, 11, 3), D(2026, 12, 3), comment="პირადი", available=30)            # 31
    assert codes(r) == ["unpaid_annual_limit"] and r.violations[0].redirect_to == "hr"


def test_unpaid_limit_counts_earlier_unpaid(check):
    r = check("UNPAID", D(2026, 11, 3), D(2026, 11, 12), comment="პირადი", available=5)
    assert codes(r) == ["unpaid_annual_limit"]
