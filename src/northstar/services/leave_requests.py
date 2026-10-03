"""Leave request use cases behind the MCP tools.

Creation is a two-step, idempotent flow (data dictionary "conversation and confirmation"):
1. `propose` validates a draft with the policy rules and stores a `leave_proposals` row;
   nothing is written to `leave_requests`.
2. `confirm` (after the employee's explicit "yes") re-validates and inserts exactly one
   pending request. Confirming the same proposal again returns the original request.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy.orm import Session

from northstar.clock import Clock, default_clock
from northstar.database.models import LeaveProposal, LeaveRequest
from northstar.database.repositories import (
    EmployeeRepository,
    LeaveRequestRepository,
    LeaveTypeRepository,
    ProposalRepository,
    RequestFilter,
)
from northstar.domain.enums import CreatedVia, RequestStatus
from northstar.domain.errors import (
    EntitlementNotFound,
    InvalidDateRange,
    InvalidStatusTransition,
    ProposalMismatch,
    ProposalNotConfirmable,
    ProposalNotFound,
    ProposalRulesChanged,
    RequestNotFound,
    UnknownLeaveType,
)
from northstar.domain.models import (
    BalanceList,
    CreateRequestOut,
    DecisionOut,
    EmployeeProfile,
    LeaveRequestList,
    LeaveRequestOut,
    LeaveTypeList,
    LeaveTypeOut,
    ProposalOut,
    ProposalStatusOut,
    ViolationOut,
)
from northstar.policies.rules import LeaveDraft, RuleResult
from northstar.services.authorization import Principal, require_hr, resolve_target_employee
from northstar.services.balance import BalanceService
from northstar.services.leave_rules import LeaveRuleService

PROPOSAL_TTL = timedelta(minutes=30)
MAX_LIST_LIMIT = 200


def request_out(r: LeaveRequest) -> LeaveRequestOut:
    return LeaveRequestOut(
        request_id=r.request_id, employee_id=r.employee_id, leave_type=r.leave_type, start_date=r.start_date,
        end_date=r.end_date, days=r.days, status=r.status, created_at=r.created_at, created_via=r.created_via,
        comment=r.comment, decided_at=r.decided_at, decided_by=r.decided_by, decision_reason=r.decision_reason,
    )


def _violations(result: RuleResult) -> list[ViolationOut]:
    return [ViolationOut(**v.to_dict()) for v in result.violations]


def _outcome(result: RuleResult) -> str:
    if result.ok:
        return "awaiting_confirmation"
    if all(v.kind == "needs_input" for v in result.violations):
        return "needs_input"
    return "not_allowed"


class LeaveService:
    def __init__(self, session: Session, clock: Clock | None = None):
        self.session = session
        self.clock = clock or default_clock()
        self.employees = EmployeeRepository(session)
        self.leave_types = LeaveTypeRepository(session)
        self.requests = LeaveRequestRepository(session)
        self.proposals = ProposalRepository(session)
        self.rules = LeaveRuleService(session, self.clock)

    # --- reads ---------------------------------------------------------------------------------

    def profile(self, principal: Principal) -> EmployeeProfile:
        e = self.employees.get(principal.employee_id)
        return EmployeeProfile(
            employee_id=e.employee_id, full_name=e.full_name, department_code=e.department_code,
            department_name=e.department_name, job_title=e.job_title, probation_end_date=e.probation_end_date,
            role=principal.role.value,
        )

    def list_leave_types(self) -> LeaveTypeList:
        return LeaveTypeList(leave_types=[
            LeaveTypeOut(code=t.code, name=t.name, day_unit=t.day_unit, annual_limit_days=t.annual_limit_days,
                         self_service=t.self_service, assistant_supported=t.assistant_supported,
                         policy_reference=t.policy_reference)
            for t in self.leave_types.list_all()
        ])

    def balances(self, principal: Principal, employee_id: str | None, year: int | None,
                 leave_type: str | None) -> BalanceList:
        target = resolve_target_employee(principal, employee_id)
        year = year or self.clock.today().year
        svc = BalanceService(self.session)
        if leave_type:
            balances = [svc.get_balance(target, leave_type.upper(), year)]
        else:
            if self.employees.get(target) is None:
                svc.get_balance(target, "ANNUAL", year)  # raises EmployeeNotFound uniformly
            balances = svc.get_all_balances(target, year)
            if not balances:
                raise EntitlementNotFound(f"{year} წლისთვის კუთვნილი დღეების მონაცემი HR სისტემაში არ არის.",
                                          details={"year": year})
        return BalanceList(employee_id=target, year=year, balances=balances)

    def list_requests(self, principal: Principal, employee_id: str | None, statuses: list[str] | None,
                      leave_type: str | None, date_from: date | None, date_to: date | None,
                      limit: int = 50) -> LeaveRequestList:
        # Employees are always scoped to themselves; HR may omit employee_id to search everyone.
        target = (employee_id.strip().upper() if employee_id else None) if principal.is_hr \
            else resolve_target_employee(principal, employee_id)
        if date_from and date_to and date_to < date_from:
            raise InvalidDateRange("თარიღების დიაპაზონი არასწორია: „დან“ თარიღი „მდე“ თარიღზე გვიანაა.")
        rows = self.requests.list(RequestFilter(
            employee_id=target, statuses=tuple(statuses) if statuses else None,
            leave_type=leave_type.upper() if leave_type else None,
            date_from=date_from, date_to=date_to, limit=max(1, min(limit, MAX_LIST_LIMIT)),
        ))
        return LeaveRequestList(requests=[request_out(r) for r in rows], count=len(rows))

    # --- proposal / creation (employee's own requests only) ------------------------------------

    def propose(self, principal: Principal, conversation_id: uuid.UUID, leave_type: str, start_date: date,
                end_date: date, comment: str | None, sick_period_known_in_advance: bool) -> ProposalOut:
        code = leave_type.strip().upper()
        lt = self.leave_types.get(code)
        if lt is None:
            raise UnknownLeaveType("შვებულების ასეთი სახე არ არსებობს.", details={"leave_type": leave_type})
        comment = (comment or "").strip() or None
        draft = LeaveDraft(code, start_date, end_date, comment, sick_period_known_in_advance)
        result, ctx = self.rules.evaluate(principal.employee_id, draft)

        out = ProposalOut(
            outcome=_outcome(result), conversation_id=conversation_id, leave_type=code, leave_type_name=lt.name,
            start_date=start_date, end_date=end_date, days=result.days, day_unit=lt.day_unit, comment=comment,
            available_days_before=ctx.available_days, violations=_violations(result),
        )
        if not result.ok:
            return out

        # Only one open proposal per conversation: a new one supersedes earlier unconfirmed ones.
        now = self.clock.now()
        for old in self.proposals.open_for_conversation(principal.employee_id, conversation_id):
            old.status, old.resolved_at = "declined", now
        proposal = self.proposals.add(LeaveProposal(
            proposal_id=uuid.uuid4(), conversation_id=conversation_id, employee_id=principal.employee_id,
            leave_type=code, start_date=start_date, end_date=end_date, days=result.days, comment=comment,
            status="proposed", created_at=now, expires_at=now + PROPOSAL_TTL,
        ))
        out.proposal_id = proposal.proposal_id
        out.expires_at = proposal.expires_at
        if ctx.available_days is not None:
            out.available_days_after = ctx.available_days - result.days
        return out

    def confirm(self, principal: Principal, proposal_id: uuid.UUID, leave_type: str, start_date: date,
                end_date: date, comment: str | None) -> CreateRequestOut:
        """Create the request the employee explicitly confirmed (create_leave_request).

        The request data must equal the confirmed proposal: the summary the employee said "yes" to is
        exactly what is stored. The employee always comes from the authenticated principal.
        """
        proposal = self.proposals.get(proposal_id, for_update=True)  # serialises concurrent confirmations
        if proposal is None or proposal.employee_id != principal.employee_id:
            # Another employee's proposal is reported exactly like a missing one.
            raise ProposalNotFound("შეთავაზება ვერ მოიძებნა.")
        requested = (leave_type.strip().upper(), start_date, end_date, (comment or "").strip() or None)
        if requested != (proposal.leave_type, proposal.start_date, proposal.end_date, proposal.comment):
            raise ProposalMismatch(
                "მოთხოვნის მონაცემები არ ემთხვევა დადასტურებულ შეჯამებას. მოთხოვნა არ შეიქმნა.",
                details={"proposal": {"leave_type": proposal.leave_type, "start_date": proposal.start_date.isoformat(),
                                      "end_date": proposal.end_date.isoformat()}})

        if proposal.status == "confirmed":
            existing = self.requests.get(proposal.created_request_id)
            return CreateRequestOut(request=request_out(existing), proposal_id=proposal_id, already_existed=True)

        now = self.clock.now()
        if proposal.status == "proposed" and proposal.expires_at <= now:
            proposal.status, proposal.resolved_at = "expired", now
            self.session.flush()
        if proposal.status != "proposed":
            raise ProposalNotConfirmable(
                "ეს შეთავაზება აღარ არის აქტიური (ვადა გავიდა ან გაუქმდა). საჭიროების შემთხვევაში თავიდან მოითხოვეთ.",
                details={"status": proposal.status})

        # Re-check every rule: balance, overlap or notice may have changed since the proposal.
        draft = LeaveDraft(proposal.leave_type, proposal.start_date, proposal.end_date, proposal.comment,
                           sick_period_known_in_advance=True)
        result, _ = self.rules.evaluate(principal.employee_id, draft)
        if not result.ok or result.days != proposal.days:
            raise ProposalRulesChanged(
                "მოთხოვნის შექმნა ვეღარ მოხერხდება, რადგან პირობები შეიცვალა.",
                details={"violations": [v.to_dict() for v in result.violations]})

        request = self.requests.add(LeaveRequest(
            employee_id=principal.employee_id, leave_type=proposal.leave_type, start_date=proposal.start_date,
            end_date=proposal.end_date, days=proposal.days, status=RequestStatus.PENDING.value, created_at=now,
            created_via=CreatedVia.ASSISTANT.value, comment=proposal.comment, proposal_id=proposal.proposal_id,
        ))
        proposal.status, proposal.created_request_id, proposal.resolved_at = "confirmed", request.request_id, now
        self.session.flush()
        return CreateRequestOut(request=request_out(request), proposal_id=proposal_id, already_existed=False)

    def decline(self, principal: Principal, proposal_id: uuid.UUID) -> ProposalStatusOut:
        proposal = self.proposals.get(proposal_id, for_update=True)
        if proposal is None or proposal.employee_id != principal.employee_id:
            raise ProposalNotFound("შეთავაზება ვერ მოიძებნა.")
        if proposal.status == "proposed":
            proposal.status, proposal.resolved_at = "declined", self.clock.now()
        return ProposalStatusOut(proposal_id=proposal_id, status=proposal.status)

    # --- HR decisions --------------------------------------------------------------------------

    def _transition(self, principal: Principal, request_id: int, action: str, allowed_from: set[str],
                    new_status: str, reason: str | None) -> DecisionOut:
        require_hr(principal, action)
        request = self.requests.get(request_id, for_update=True)
        if request is None:
            raise RequestNotFound("მოთხოვნა ვერ მოიძებნა.", details={"request_id": request_id})
        if request.status not in allowed_from:
            raise InvalidStatusTransition(
                f"მოთხოვნის სტატუსია „{request.status}“, ამიტომ ეს მოქმედება შეუძლებელია.",
                details={"request_id": request_id, "status": request.status})
        previous = request.status
        request.status = new_status
        request.decided_at = self.clock.now()
        request.decided_by = principal.employee_id
        request.decision_reason = (reason or "").strip() or None
        self.session.flush()
        return DecisionOut(request=request_out(request), previous_status=previous)

    def approve(self, principal: Principal, request_id: int, comment: str | None) -> DecisionOut:
        return self._transition(principal, request_id, "დამტკიცება", {"pending"}, "approved", comment)

    def reject(self, principal: Principal, request_id: int, reason: str) -> DecisionOut:
        require_hr(principal, "უარყოფა")
        if not reason or not reason.strip():
            raise InvalidStatusTransition("უარის მიზეზის მითითება სავალდებულოა (მუხლი 13.2).", article="13.2")
        return self._transition(principal, request_id, "უარყოფა", {"pending"}, "rejected", reason)

    def cancel(self, principal: Principal, request_id: int, reason: str | None) -> DecisionOut:
        return self._transition(principal, request_id, "გაუქმება", {"pending", "approved"}, "cancelled", reason)
