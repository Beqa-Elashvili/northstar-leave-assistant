"""Business date provider.

Leave rules (notice periods, current leave year, probation, submission date) must be
reproducible, so they never call `date.today()` / `datetime.now()` directly. They use a
`Clock`, which by default reads `APP_TODAY` from the settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from northstar.config import get_settings


class Clock(Protocol):
    def today(self) -> date: ...

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class FixedDateClock:
    """Business date is fixed; the time of day comes from the real clock in the company time zone.

    `now()` therefore always falls on `today()`, which keeps `created_at` consistent with the
    submission date used for the notice-period rules.
    """

    business_date: date
    tz: ZoneInfo

    def today(self) -> date:
        return self.business_date

    def now(self) -> datetime:
        wall = datetime.now(self.tz).time().replace(microsecond=0)
        return datetime.combine(self.business_date, wall, tzinfo=self.tz)


@dataclass(frozen=True)
class FrozenClock:
    """Fully deterministic clock for tests."""

    moment: datetime

    def today(self) -> date:
        return self.moment.date()

    def now(self) -> datetime:
        return self.moment


def default_clock() -> Clock:
    settings = get_settings()
    return FixedDateClock(settings.app_today, settings.tz)


def get_app_today() -> date:
    return default_clock().today()
