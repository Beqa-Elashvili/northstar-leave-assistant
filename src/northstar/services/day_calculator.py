"""Central leave-day arithmetic. No other module does date counting for leave rules.

Definitions (Leave and Absence Policy v4.0):
- working day (1.3, 2.2): Monday–Friday that is not in the official HR holiday list (2.3);
- calendar day (1.3, 7.1): every day, including weekends and holidays;
- notice (4.4): complete working days strictly between the submission day and the first
  leave day; both of those days are excluded;
- continuity (4.5): two periods form one continuous leave when only weekends or holidays
  lie between them;
- leave year (2.1): the calendar year of the request's first day.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from northstar.domain.enums import DayUnit
from northstar.domain.errors import InvalidDateRange

_ONE_DAY = timedelta(days=1)
# Safety bound for searches (a notice period can never need more than a few months).
_MAX_SEARCH_DAYS = 400


def _days(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += _ONE_DAY


class LeaveDayCalculator:
    def __init__(self, holidays: Iterable[date]):
        # Only the official HR list is used (policy 2.3); no holiday libraries or guesses.
        self._holidays = frozenset(holidays)

    @property
    def holidays(self) -> frozenset[date]:
        return self._holidays

    def is_holiday(self, day: date) -> bool:
        return day in self._holidays

    def is_working_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self._holidays

    @staticmethod
    def validate_range(start: date, end: date) -> None:
        if end < start:
            raise InvalidDateRange(
                "დასრულების თარიღი დაწყების თარიღზე ადრეა.",
                details={"start_date": start.isoformat(), "end_date": end.isoformat()},
            )

    def working_days(self, start: date, end: date) -> int:
        """Working days in [start, end], both inclusive."""
        self.validate_range(start, end)
        return sum(1 for d in _days(start, end) if self.is_working_day(d))

    def calendar_days(self, start: date, end: date) -> int:
        """Calendar days in [start, end], both inclusive."""
        self.validate_range(start, end)
        return (end - start).days + 1

    def count(self, day_unit: DayUnit | str | None, start: date, end: date) -> int:
        if day_unit == DayUnit.WORKING:
            return self.working_days(start, end)
        if day_unit == DayUnit.CALENDAR:
            return self.calendar_days(start, end)
        # PARENTAL has no day unit: its duration is set individually by HR (policy 10).
        raise ValueError(f"leave type with day unit {day_unit!r} cannot be counted automatically")

    def working_days_between(self, first: date, second: date) -> int:
        """Working days strictly between two dates (both excluded); 0 if they are adjacent or reversed."""
        if second <= first + _ONE_DAY:
            return 0
        return self.working_days(first + _ONE_DAY, second - _ONE_DAY)

    def notice_working_days(self, submission: date, leave_start: date) -> int:
        """Advance notice as defined in policy 4.4 (also used for UNPAID 7.2 and STUDY 9.3)."""
        return self.working_days_between(submission, leave_start)

    def earliest_start_with_notice(self, submission: date, required_notice: int) -> date:
        """First date whose notice from `submission` is at least `required_notice` working days."""
        candidate = submission + _ONE_DAY
        for _ in range(_MAX_SEARCH_DAYS):
            if self.notice_working_days(submission, candidate) >= required_notice:
                return candidate
            candidate += _ONE_DAY
        raise ValueError("notice period search exceeded bound")

    def are_continuous(self, earlier_end: date, later_start: date) -> bool:
        """True when nothing but weekends/holidays separates two periods (policy 4.5).

        Overlapping or directly adjacent periods are continuous too.
        """
        if later_start <= earlier_end + _ONE_DAY:
            return True
        return self.working_days_between(earlier_end, later_start) == 0

    @staticmethod
    def leave_year(start: date) -> int:
        return start.year

    @staticmethod
    def spans_single_year(start: date, end: date) -> bool:
        return start.year == end.year
