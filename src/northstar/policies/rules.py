"""Deterministic leave-request rules for the employee assistant (Leave and Absence Policy v4.0).

`evaluate()` is pure: it receives everything it needs in a `RuleContext` and returns a
`RuleResult`. The LLM never decides any of this; the MCP server runs these rules on every
proposal and again at confirmation time.

Outcome kinds of a violation:
- `rejected`    the request cannot be created as asked (e.g. notice too short, overlap);
- `redirect`    the case belongs to another channel (HR, manager) by policy;
- `needs_input` the assistant must ask the employee for something first (reason, confirmation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from northstar.domain.enums import LeaveTypeCode
from northstar.services.day_calculator import LeaveDayCalculator

Kind = Literal["rejected", "redirect", "needs_input"]
Channel = Literal["hr", "hr_portal", "manager"]

ASSISTANT_CREATABLE = frozenset({LeaveTypeCode.ANNUAL, LeaveTypeCode.SICK, LeaveTypeCode.UNPAID})
ANNUAL_MAX_WORKING_DAYS = 15                       # 4.5
ANNUAL_NOTICE = ((5, 5), (15, 15))                 # 4.4: (max requested days, required notice)
UNPAID_NOTICE_WORKING_DAYS = 10                    # 7.2
SICK_LATE_SUBMISSION_WORKING_DAYS = 2              # 6.2
AUDIT_DEPARTMENT = "AUD"
# 4.6 restricted periods for the audit department, (month, day) inclusive.
AUDIT_RESTRICTED_PERIODS = (((12, 1), (12, 20)), ((1, 15), (3, 15)))
MAX_COMMENT_LENGTH = 500

# Conservative guard for policy 12.4 / data dictionary: health details are not collected.
_HEALTH_TERMS = re.compile(
    r"ავად|ავადმყოფ|ექიმ|დიაგნოზ|ოპერაცი|საავადმყოფო|ჰოსპიტალ|მკურნალ|სიმპტომ|ტემპერატურ|ორსულ|"
    r"\b(sick|ill|illness|doctor|diagnos|surgery|hospital|pregnan)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EmployeeInfo:
    employee_id: str
    department_code: str
    employment_type: str
    status: str
    probation_end_date: date


@dataclass(frozen=True)
class LeaveTypeInfo:
    code: str
    name: str
    day_unit: str | None
    assistant_supported: bool
    self_service: bool


@dataclass(frozen=True)
class ExistingRequest:
    request_id: int
    leave_type: str
    start_date: date
    end_date: date
    days: int
    status: str


@dataclass(frozen=True)
class LeaveDraft:
    leave_type: str
    start_date: date
    end_date: date
    comment: str | None = None
    # SICK with future dates: the employee confirms the period is already known (6.2, 12.4).
    sick_period_known_in_advance: bool = False


@dataclass(frozen=True)
class RuleContext:
    today: date
    employee: EmployeeInfo
    leave_type: LeaveTypeInfo
    calculator: LeaveDayCalculator
    # The employee's own approved + pending requests (all types).
    active_requests: tuple[ExistingRequest, ...]
    # Available days for this type and leave year; None when the type has no balance.
    available_days: int | None


@dataclass(frozen=True)
class Violation:
    code: str
    kind: Kind
    message: str
    article: str
    redirect_to: Channel | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code, "kind": self.kind, "message": self.message, "article": self.article,
            "redirect_to": self.redirect_to, "details": self.details,
        }


@dataclass(frozen=True)
class RuleResult:
    days: int | None
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


def _fmt(d: date) -> str:
    return d.isoformat()


# --- assistant capability ------------------------------------------------------------------------

def unsupported_type_violation(leave_type: LeaveTypeInfo) -> Violation:
    """Explanation + redirect for types the assistant must not create (12.2, 12.3)."""
    code = leave_type.code
    if code == LeaveTypeCode.BEREAVEMENT:
        return Violation(
            "assistant_unsupported_type", "redirect",
            "გლოვის შვებულების მოთხოვნას ასისტენტი ვერ ქმნის. მოთხოვნა წარადგინეთ HR პორტალით ან ადამიანური "
            "რესურსების სამსახურის მეშვეობით, არაუგვიანეს 2 სამუშაო დღისა შვებულების პირველი დღიდან. "
            "ახლო ოჯახის წევრის გარდაცვალებისას გეკუთვნით არაუმეტეს 3 სამუშაო დღე, ბებიის, ბაბუის, "
            "შვილიშვილის, მეუღლის მშობლისა და მეუღლის და-ძმის შემთხვევაში 1 სამუშაო დღე; შვებულება "
            "გამოიყენება გარდაცვალებიდან 30 კალენდარული დღის განმავლობაში.",
            "8.1–8.3", "hr_portal", {"leave_type": code},
        )
    if code == LeaveTypeCode.STUDY:
        return Violation(
            "assistant_unsupported_type", "redirect",
            "სასწავლო შვებულების მოთხოვნას ასისტენტი ვერ ქმნის. შვებულების წელიწადში გეკუთვნით არაუმეტეს "
            "5 სამუშაო დღე მხოლოდ იმ კვალიფიკაციის გამოცდებისთვის, რომელიც თქვენს დამტკიცებულ სწავლისა და "
            "განვითარების გეგმაშია; გეგმასთან შესაბამისობას HR ამოწმებს. მოთხოვნა წარადგინეთ HR პორტალით ან "
            "HR-ის მეშვეობით, დაწყებამდე სულ მცირე 10 სამუშაო დღით ადრე, გამოცდის დასახელებისა და თარიღის "
            "მითითებით.",
            "9.1–9.3", "hr_portal", {"leave_type": code},
        )
    if code == LeaveTypeCode.PARENTAL:
        return Violation(
            "assistant_unsupported_type", "redirect",
            "მშობლის შვებულება (დედობის, ბავშვის მოვლის, მამობის, შვილად აყვანის) HR პორტალით ან ასისტენტით "
            "არ წარდგება. მიმართეთ პირდაპირ ადამიანური რესურსების სამსახურს არაუგვიანეს 8 კვირით ადრე "
            "სავარაუდო დაწყებამდე ან როგორც კი შეძლებთ; HR თქვენთან ერთად განსაზღვრავს ხანგრძლივობას, "
            "ანაზღაურებასა და საჭირო დოკუმენტებს.",
            "10.1–10.2", "hr", {"leave_type": code},
        )
    return Violation(
        "assistant_unsupported_type", "redirect",
        f"„{leave_type.name}“-ის მოთხოვნას ასისტენტი ვერ ქმნის. მიმართეთ HR პორტალს ან ადამიანური რესურსების სამსახურს.",
        "12.3", "hr_portal" if leave_type.self_service else "hr", {"leave_type": code},
    )


# --- evaluation -----------------------------------------------------------------------------------

def evaluate(draft: LeaveDraft, ctx: RuleContext) -> RuleResult:
    calc = ctx.calculator
    lt = ctx.leave_type

    # 1. Leave types the assistant may not create: explain and redirect, nothing else matters.
    if not lt.assistant_supported or lt.code not in ASSISTANT_CREATABLE:
        return RuleResult(None, (unsupported_type_violation(lt),))

    # 2. Structural checks; later rules are meaningless when these fail.
    structural = _structural(draft, ctx)
    if structural:
        return RuleResult(None, tuple(structural))

    days = calc.count(lt.day_unit, draft.start_date, draft.end_date)
    if days == 0:
        return RuleResult(0, (Violation(
            "no_working_days", "rejected",
            "არჩეულ პერიოდში სამუშაო დღე არ არის (მხოლოდ შაბათ-კვირა ან უქმე დღეებია).", "2.2",
        ),))

    violations: list[Violation] = []
    violations += _overlap(draft, ctx)
    if lt.code == LeaveTypeCode.ANNUAL:
        violations += _annual(draft, ctx, days)
    elif lt.code == LeaveTypeCode.SICK:
        violations += _sick(draft, ctx, days)
    elif lt.code == LeaveTypeCode.UNPAID:
        violations += _unpaid(draft, ctx, days)
    return RuleResult(days, tuple(violations))


def _structural(draft: LeaveDraft, ctx: RuleContext) -> list[Violation]:
    emp = ctx.employee
    if emp.status != "active":
        return [Violation("employee_not_active", "redirect",
                          "თქვენი სტატუსი HR სისტემაში აქტიური არ არის. მიმართეთ ადამიანური რესურსების სამსახურს.",
                          "1.2", "hr")]
    if emp.employment_type != "full_time":
        return [Violation("not_full_time", "redirect",
                          "ეს პოლიტიკა ვრცელდება მხოლოდ სრულ განაკვეთზე დასაქმებულებზე; თქვენი პირობები "
                          "ინდივიდუალურად განისაზღვრება. მიმართეთ ადამიანური რესურსების სამსახურს.", "1.2", "hr")]
    if draft.end_date < draft.start_date:
        return [Violation("invalid_date_range", "rejected", "დასრულების თარიღი დაწყების თარიღზე ადრეა.", "2.4",
                          details={"start_date": _fmt(draft.start_date), "end_date": _fmt(draft.end_date)})]
    if not ctx.calculator.spans_single_year(draft.start_date, draft.end_date):
        return [Violation("crosses_leave_year", "rejected",
                          "მოთხოვნა ერთ შვებულების წელს უნდა ეკუთვნოდეს: წლიდან წელზე გადასული შვებულებისთვის "
                          "თითოეული წლისთვის ცალკე მოთხოვნა წარადგინეთ.", "2.1")]
    if draft.start_date.year != ctx.today.year:
        if draft.start_date.year > ctx.today.year:
            message = (f"ასისტენტი მხოლოდ მიმდინარე ({ctx.today.year}) წლის თარიღებზე ქმნის მოთხოვნას. მომდევნო "
                       f"წლის მოთხოვნა HR პორტალით შეგიძლიათ წარადგინოთ {ctx.today.year} წლის 1 დეკემბრიდან.")
            return [Violation("not_current_year", "redirect", message, "12.3", "hr_portal")]
        return [Violation("not_current_year", "rejected",
                          f"ასისტენტი მხოლოდ მიმდინარე ({ctx.today.year}) წლის თარიღებზე ქმნის მოთხოვნას.", "12.3")]
    return []


def _overlap(draft: LeaveDraft, ctx: RuleContext) -> list[Violation]:
    clashes = [r for r in ctx.active_requests
               if r.start_date <= draft.end_date and r.end_date >= draft.start_date]
    if not clashes:
        return []
    status_ka = {"approved": "დამტკიცებული", "pending": "განხილვის პროცესში"}
    listed = "; ".join(f"#{r.request_id} {r.leave_type} {_fmt(r.start_date)} – {_fmt(r.end_date)} "
                       f"({status_ka.get(r.status, r.status)})" for r in clashes)
    return [Violation("overlapping_request", "rejected",
                      f"არჩეული თარიღები ემთხვევა თქვენს სხვა მოთხოვნას: {listed}. აირჩიეთ სხვა თარიღები; არსებული "
                      "მოთხოვნის შეცვლა ან გაუქმება შესაძლებელია მხოლოდ HR პორტალით.", "12.3",
                      details={"request_ids": [r.request_id for r in clashes]})]


def _notice(draft: LeaveDraft, ctx: RuleContext, required: int, article: str, label: str) -> list[Violation]:
    actual = ctx.calculator.notice_working_days(ctx.today, draft.start_date)
    if actual >= required:
        return []
    earliest = ctx.calculator.earliest_start_with_notice(ctx.today, required)
    message = (f"{label} საჭიროა სულ მცირე {required} სამუშაო დღით ადრე წარდგენა (წარდგენისა და დაწყების დღეები "
               f"არ ითვლება). {_fmt(draft.start_date)}-მდე მხოლოდ {max(actual, 0)} სრული სამუშაო დღეა. ყველაზე "
               f"ადრეული შესაძლო დაწყების თარიღია {_fmt(earliest)}.")
    if article == "4.4":
        message += (" გადაუდებელი პირადი მიზეზის შემთხვევაში მოთხოვნა პირდაპირ უშუალო ხელმძღვანელს გაუგზავნეთ — "
                    "ასისტენტი ამ გამონაკლისს ვერ გამოიყენებს.")
    return [Violation("notice_period", "rejected", message, article,
                      details={"required_working_days": required, "actual_working_days": max(actual, 0),
                               "earliest_start_date": _fmt(earliest)})]


def _annual(draft: LeaveDraft, ctx: RuleContext, days: int) -> list[Violation]:
    out: list[Violation] = []

    # 4.3 probation (end date inclusive). The HR 2-day exception is not available via the assistant.
    probation_end = ctx.employee.probation_end_date
    if draft.start_date <= probation_end:
        first_allowed = probation_end + timedelta(days=1)
        out.append(Violation(
            "probation", "redirect",
            f"გამოსაცდელი ვადის განმავლობაში ({_fmt(probation_end)}-ის ჩათვლით) ყოველწლიური შვებულების გამოყენება "
            f"არ შეიძლება; კუთვნილი დღეები გერიცხებათ და მათი გამოყენება {_fmt(first_allowed)}-დან შეგიძლიათ. "
            "გამონაკლის შემთხვევაში (არაუმეტეს 2 სამუშაო დღე) მიმართეთ პირდაპირ ადამიანური რესურსების სამსახურს — "
            "ასეთი მოთხოვნა ასისტენტით არ იგზავნება.", "4.3", "hr",
            {"probation_end_date": _fmt(probation_end), "earliest_start_date": _fmt(first_allowed)}))

    # 4.5 maximum length of one request and of a continuous chain of requests.
    if days > ANNUAL_MAX_WORKING_DAYS:
        out.append(_too_long(days, []))
    else:
        chain_ids, chain_days = _continuous_annual_chain(draft, ctx)
        if chain_ids and days + chain_days > ANNUAL_MAX_WORKING_DAYS:
            out.append(_too_long(days + chain_days, chain_ids))

    # 4.4 notice depends on the requested length (only defined up to 15 days).
    if days <= ANNUAL_MAX_WORKING_DAYS:
        required = next(n for limit, n in ANNUAL_NOTICE if days <= limit)
        out += _notice(draft, ctx, required, "4.4", f"{days} სამუშაო დღის ყოველწლიური შვებულებისთვის")

    # 4.6 audit department restricted periods: any day of the requested period.
    if ctx.employee.department_code == AUDIT_DEPARTMENT:
        hits = _restricted_days(draft.start_date, draft.end_date)
        if hits:
            out.append(Violation(
                "restricted_period", "redirect",
                "აუდიტისა და მარწმუნებელი მომსახურების დეპარტამენტისთვის შეზღუდულია 1–20 დეკემბერი და 15 იანვარი – "
                "15 მარტი. თუ მოთხოვნილი პერიოდის რომელიმე დღე ამ პერიოდშია, მოთხოვნა HR პორტალით ან ასისტენტით არ "
                "იგზავნება: მიმართეთ უშუალო ხელმძღვანელს, რომელიც საკითხს პროექტის პარტნიორთან შეათანხმებს.",
                "4.6", "manager", {"first_restricted_day": _fmt(hits[0]), "restricted_days": len(hits)}))

    # 5.1 balance (pending requests already reduce it).
    if ctx.available_days is not None and days > ctx.available_days:
        out.append(Violation(
            "insufficient_balance", "rejected",
            f"ხელმისაწვდომი ბალანსი არასაკმარისია: მოთხოვნილია {days} სამუშაო დღე, ხელმისაწვდომია "
            f"{max(ctx.available_days, 0)} (განხილვის პროცესში მყოფი მოთხოვნების გათვალისწინებით).", "5.1",
            details={"requested_days": days, "available_days": ctx.available_days}))
    return out


def _too_long(total: int, chain_ids: list[int]) -> Violation:
    if chain_ids:
        joined = ", ".join(f"#{i}" for i in chain_ids)
        lead = (f"ეს მოთხოვნა თქვენს მოთხოვნ(ებ)თან {joined} ერთ უწყვეტ შვებულებას ქმნის (მათ შორის მხოლოდ შაბათ-კვირა "
                f"ან უქმე დღეებია) — ჯამში {total} სამუშაო დღე.")
    else:
        lead = f"მოთხოვნილია {total} სამუშაო დღე."
    return Violation(
        "annual_max_length", "redirect",
        f"{lead} ერთი მოთხოვნით შესაძლებელია არაუმეტეს {ANNUAL_MAX_WORKING_DAYS} სამუშაო დღის ყოველწლიური შვებულება და "
        "ამ ზღვრის გვერდის ავლა რამდენიმე მოთხოვნით დაუშვებელია. უფრო ხანგრძლივი უწყვეტი შვებულებისთვის საჭიროა "
        "პარტნიორის ან დირექტორის წერილობითი თანხმობა; მოთხოვნა უშუალო ხელმძღვანელის მეშვეობით წარადგინეთ.",
        "4.5", "manager", {"total_working_days": total, "chained_request_ids": chain_ids})


def _continuous_annual_chain(draft: LeaveDraft, ctx: RuleContext) -> tuple[list[int], int]:
    """Active ANNUAL requests that form one continuous period with the draft (transitively, 4.5)."""
    calc = ctx.calculator
    others = [r for r in ctx.active_requests if r.leave_type == LeaveTypeCode.ANNUAL]
    start, end = draft.start_date, draft.end_date
    chain: dict[int, ExistingRequest] = {}
    grew = True
    while grew:  # the block grows as requests join, so re-scan until stable
        grew = False
        for r in others:
            if r.request_id in chain:
                continue
            before = r.end_date < start and calc.are_continuous(r.end_date, start)
            after = r.start_date > end and calc.are_continuous(end, r.start_date)
            if before or after:
                chain[r.request_id] = r
                start, end = min(start, r.start_date), max(end, r.end_date)
                grew = True
    return sorted(chain), sum(r.days for r in chain.values())


def _restricted_days(start: date, end: date) -> list[date]:
    hits = []
    current = start
    while current <= end:
        md = (current.month, current.day)
        if any(lo <= md <= hi for lo, hi in AUDIT_RESTRICTED_PERIODS):
            hits.append(current)
        current += timedelta(days=1)
    return hits


def _sick(draft: LeaveDraft, ctx: RuleContext, days: int) -> list[Violation]:
    calc = ctx.calculator
    out: list[Violation] = []
    if draft.start_date <= ctx.today:
        # 6.2: record on the first day or within 2 working days after it (first day not counted).
        elapsed = calc.working_days(draft.start_date + timedelta(days=1), ctx.today) if draft.start_date < ctx.today else 0
        if elapsed > SICK_LATE_SUBMISSION_WORKING_DAYS:
            out.append(Violation(
                "sick_late_submission", "redirect",
                "ავადმყოფობის შვებულება აღირიცხება პირველ დღეს ან, თუ ეს ვერ მოხერხდა, არაუგვიანეს 2 სამუშაო დღისა "
                "პირველი დღიდან. ეს ვადა გასულია, ამიტომ ასისტენტი მოთხოვნას ვერ შექმნის — მიმართეთ ადამიანური "
                "რესურსების სამსახურს.", "6.2", "hr", {"working_days_since_start": elapsed}))
    elif not draft.sick_period_known_in_advance:
        out.append(Violation(
            "sick_future_confirmation", "needs_input",
            "მომავალი თარიღით ავადმყოფობის შვებულება აღირიცხება მხოლოდ მაშინ, როცა გაცდენის პერიოდი წინასწარ არის "
            "ცნობილი (მაგალითად, ექიმის დოკუმენტით ან დაგეგმილი ოპერაციის შემთხვევაში). გთხოვთ, დაადასტუროთ, რომ "
            "პერიოდი წინასწარ არის ცნობილი — ჯანმრთელობის დეტალების მითითება საჭირო არ არის.", "6.2"))

    # 6.4: more than the remaining paid days -> not created by the assistant, HR decides.
    available = ctx.available_days if ctx.available_days is not None else 0
    if days > available:
        out.append(Violation(
            "sick_paid_limit_exceeded", "redirect",
            f"მოთხოვნილი {days} სამუშაო დღე აღემატება თქვენს დარჩენილ ანაზღაურებად ავადმყოფობის დღეებს ({available}). "
            "ასეთ მოთხოვნას ასისტენტი არ ქმნის: მიმართეთ ადამიანური რესურსების სამსახურს, რომელიც მოთხოვნის "
            "აღრიცხვასა და ანაზღაურების პირობებს ინდივიდუალურად განსაზღვრავს. ეს არ ნიშნავს, რომ ავადმყოფობის "
            "აღრიცხვა აღარ შეიძლება — 10 დღე მხოლოდ ანაზღაურების ლიმიტია.", "6.4", "hr",
            {"requested_days": days, "available_paid_days": available}))
    return out


def _unpaid(draft: LeaveDraft, ctx: RuleContext, days: int) -> list[Violation]:
    out: list[Violation] = []
    reason = (draft.comment or "").strip()
    if not reason:
        out.append(Violation(
            "reason_required", "needs_input",
            "უხელფასო შვებულებისთვის მოკლედ მიუთითეთ მიზეზი (ჯანმრთელობის დეტალების გარეშე).", "7.2"))
    elif _HEALTH_TERMS.search(reason):
        out.append(Violation(
            "health_details_in_reason", "needs_input",
            "მიზეზში ჯანმრთელობის დეტალებს ნუ მიუთითებთ — ასისტენტი ასეთ ინფორმაციას არ აგროვებს. თუ გაცდენა "
            "ავადმყოფობის გამოა, გამოიყენეთ ავადმყოფობის შვებულება; სხვა შემთხვევაში მიზეზი ზოგადად ჩამოაყალიბეთ "
            "(მაგალითად, „პირადი/ოჯახური მიზეზი“).", "12.4"))
    elif len(reason) > MAX_COMMENT_LENGTH:
        out.append(Violation(
            "reason_too_long", "needs_input",
            f"მიზეზი მოკლედ ჩამოაყალიბეთ (არაუმეტეს {MAX_COMMENT_LENGTH} სიმბოლო).", "7.2"))

    out += _notice(draft, ctx, UNPAID_NOTICE_WORKING_DAYS, "7.2", "უხელფასო შვებულებისთვის")

    if ctx.available_days is not None and days > ctx.available_days:
        out.append(Violation(
            "unpaid_annual_limit", "redirect",
            f"წელიწადში ჯამურად შესაძლებელია არაუმეტეს 30 კალენდარული დღის უხელფასო შვებულება; მოთხოვნილია {days} "
            f"კალენდარული დღე, ხელმისაწვდომია {max(ctx.available_days, 0)}. 30 დღეზე მეტი საჭიროებს მმართველი "
            "პარტნიორის თანხმობას და იგზავნება ადამიანური რესურსების სამსახურის მეშვეობით.", "7.1", "hr",
            {"requested_days": days, "available_days": ctx.available_days}))
    return out
