"""Typed domain errors.

Each error carries a stable machine `code`, a user-facing Georgian `message`, the relevant
policy `article` (when a rule is involved) and optional structured `details`. They are
safe to show to the employee: they never contain stack traces, SQL or secrets.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    code = "domain_error"

    def __init__(self, message: str, *, article: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.article = article
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "article": self.article, "details": self.details}


class InvalidDateRange(DomainError):
    code = "invalid_date_range"


class EmployeeNotFound(DomainError):
    code = "employee_not_found"


class UnknownLeaveType(DomainError):
    code = "unknown_leave_type"


class NoBalanceForLeaveType(DomainError):
    code = "no_balance_for_leave_type"


class EntitlementNotFound(DomainError):
    code = "entitlement_not_found"


class PermissionDenied(DomainError):
    code = "permission_denied"


class AuthenticationError(DomainError):
    code = "authentication_error"


class RequestNotFound(DomainError):
    code = "request_not_found"


class InvalidStatusTransition(DomainError):
    code = "invalid_status_transition"


class ProposalNotFound(DomainError):
    code = "proposal_not_found"


class ProposalNotConfirmable(DomainError):
    code = "proposal_not_confirmable"


class ProposalMismatch(DomainError):
    """create_leave_request data differs from the proposal the employee confirmed."""

    code = "proposal_mismatch"


class ProposalRulesChanged(DomainError):
    """The proposal no longer satisfies the rules at confirmation time (details carry the violations)."""

    code = "proposal_rules_changed"
