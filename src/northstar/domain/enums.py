from __future__ import annotations

from enum import StrEnum


class LeaveTypeCode(StrEnum):
    ANNUAL = "ANNUAL"
    SICK = "SICK"
    UNPAID = "UNPAID"
    BEREAVEMENT = "BEREAVEMENT"
    STUDY = "STUDY"
    PARENTAL = "PARENTAL"


class DayUnit(StrEnum):
    WORKING = "working"
    CALENDAR = "calendar"


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class CreatedVia(StrEnum):
    PORTAL = "portal"
    ASSISTANT = "assistant"
    HR = "hr"


# Policy 5.1: BEREAVEMENT and PARENTAL have no annual balance.
TYPES_WITHOUT_BALANCE = frozenset({LeaveTypeCode.BEREAVEMENT, LeaveTypeCode.PARENTAL})
