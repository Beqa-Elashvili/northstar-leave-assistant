"""LeaveDayCalculator: working/calendar days, notice, continuity (no database)."""

from datetime import date

import pytest

from northstar.domain.errors import InvalidDateRange
from northstar.services.day_calculator import LeaveDayCalculator


@pytest.fixture(scope="module")
def calc(seed_data):
    return LeaveDayCalculator(h.holiday_date for h in seed_data.holidays)


D = date


# --- working days --------------------------------------------------------------------------

def test_plain_week(calc):
    assert calc.working_days(D(2026, 10, 26), D(2026, 10, 30)) == 5   # Mon–Fri


def test_weekend_excluded(calc):
    assert calc.working_days(D(2026, 10, 30), D(2026, 11, 2)) == 2    # Fri, Sat, Sun, Mon
    assert calc.working_days(D(2026, 10, 31), D(2026, 11, 1)) == 0    # weekend only


def test_single_day(calc):
    assert calc.working_days(D(2026, 11, 2), D(2026, 11, 2)) == 1
    assert calc.working_days(D(2026, 11, 7), D(2026, 11, 7)) == 0     # Saturday


def test_public_holiday_excluded(calc):
    assert calc.working_days(D(2026, 11, 23), D(2026, 11, 27)) == 4   # Mon 11-23 გიორგობა
    assert calc.working_days(D(2026, 10, 12), D(2026, 10, 16)) == 4   # Wed 10-14 სვეტიცხოვლობა


def test_holiday_on_weekend_not_double_counted(calc):
    assert calc.is_holiday(D(2026, 3, 8))                              # Sunday
    assert calc.working_days(D(2026, 3, 2), D(2026, 3, 8)) == 4        # Tue 03-03 holiday; Sun holiday ignored


def test_easter_block_2026(calc):
    # 04-09 Thu, 04-10 Fri, 04-11 Sat, 04-12 Sun, 04-13 Mon are all holidays
    assert calc.working_days(D(2026, 4, 6), D(2026, 4, 17)) == 7


def test_policy_2_2_example(calc):
    # "Monday to the next Monday with one holiday on Tuesday = 5 working days" (2.2).
    # Week of 2027-01-18: Tue 01-19 ნათლისღება.
    assert calc.working_days(D(2027, 1, 18), D(2027, 1, 25)) == 5


def test_only_supplied_holiday_list_is_used(calc):
    assert not calc.is_holiday(D(2026, 12, 25))   # not in HR list -> ordinary working day
    assert calc.is_working_day(D(2026, 12, 25))


def test_csv_days_match_calculator(calc, seed_data):
    units = {t.code: t.day_unit for t in seed_data.leave_types}
    for r in seed_data.requests:
        assert calc.count(units[r.leave_type], r.start_date, r.end_date) == r.days, r.request_id


# --- calendar days -------------------------------------------------------------------------

def test_calendar_days_include_weekends_and_holidays(calc):
    assert calc.calendar_days(D(2026, 11, 20), D(2026, 11, 29)) == 10  # contains a weekend and 11-23
    assert calc.calendar_days(D(2026, 11, 1), D(2026, 11, 1)) == 1
    assert calc.count("calendar", D(2026, 12, 1), D(2026, 12, 31)) == 31


def test_count_dispatches_by_unit(calc):
    assert calc.count("working", D(2026, 11, 20), D(2026, 11, 29)) == 5
    with pytest.raises(ValueError):
        calc.count(None, D(2026, 11, 2), D(2026, 11, 2))  # PARENTAL


def test_reversed_range_rejected(calc):
    with pytest.raises(InvalidDateRange):
        calc.working_days(D(2026, 11, 5), D(2026, 11, 2))
    with pytest.raises(InvalidDateRange):
        calc.calendar_days(D(2026, 11, 5), D(2026, 11, 2))


# --- notice (policy 4.4) ---------------------------------------------------------------------

def test_policy_4_4_example(calc):
    # Submitted Monday, no holidays: a 3-day leave can start at the earliest next Tuesday.
    monday = D(2026, 10, 19)
    assert calc.notice_working_days(monday, D(2026, 10, 26)) == 4   # next Monday: too early
    assert calc.notice_working_days(monday, D(2026, 10, 27)) == 5   # next Tuesday: OK
    assert calc.earliest_start_with_notice(monday, 5) == D(2026, 10, 27)


def test_notice_excludes_submission_and_start_days(calc):
    assert calc.notice_working_days(D(2026, 10, 19), D(2026, 10, 20)) == 0
    assert calc.notice_working_days(D(2026, 10, 19), D(2026, 10, 19)) == 0
    assert calc.notice_working_days(D(2026, 10, 19), D(2026, 10, 21)) == 1


def test_notice_skips_holidays(calc):
    # Between 11-20 (Fri) and 11-25 (Wed): Mon 11-23 holiday, Tue 11-24 working -> 1
    assert calc.notice_working_days(D(2026, 11, 20), D(2026, 11, 25)) == 1


def test_earliest_start_for_15_and_10_day_notice(calc):
    today = D(2026, 10, 19)
    start15 = calc.earliest_start_with_notice(today, 15)
    assert calc.notice_working_days(today, start15) == 15
    assert calc.notice_working_days(today, D(2026, 11, 9)) == 14       # one day earlier is too early
    assert start15 == D(2026, 11, 10)
    assert calc.earliest_start_with_notice(today, 10) == D(2026, 11, 3)


# --- continuity (policy 4.5) and leave year (2.1) ---------------------------------------------

def test_continuity_across_weekend(calc):
    assert calc.are_continuous(D(2026, 11, 6), D(2026, 11, 9))       # Fri -> Mon


def test_continuity_across_weekend_and_holiday(calc):
    assert calc.are_continuous(D(2026, 11, 20), D(2026, 11, 24))     # Fri -> Tue, Mon 11-23 holiday


def test_not_continuous_with_working_day_between(calc):
    assert not calc.are_continuous(D(2026, 11, 6), D(2026, 11, 10))  # Mon 11-09 is a working day


def test_adjacent_or_overlapping_is_continuous(calc):
    assert calc.are_continuous(D(2026, 11, 4), D(2026, 11, 5))
    assert calc.are_continuous(D(2026, 11, 6), D(2026, 11, 3))


def test_leave_year_and_single_year(calc):
    assert calc.leave_year(D(2026, 12, 28)) == 2026
    assert calc.spans_single_year(D(2026, 12, 1), D(2026, 12, 31))
    assert not calc.spans_single_year(D(2026, 12, 28), D(2027, 1, 8))
