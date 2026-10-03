"""Structured intent extraction (LLM output schema + prompt).

The LLM only *classifies and extracts*. Every extracted value is re-validated in code, and
authorization / leave rules are enforced by the MCP server, never by this classification.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field


class Intent(StrEnum):
    POLICY_QUESTION = "POLICY_QUESTION"
    BALANCE_QUERY = "BALANCE_QUERY"
    CREATE_LEAVE_REQUEST = "CREATE_LEAVE_REQUEST"
    LIST_REQUESTS = "LIST_REQUESTS"
    MODIFY_EXISTING_REQUEST = "MODIFY_EXISTING_REQUEST"   # cancel / change / approve an existing request
    PROVIDE_DETAILS = "PROVIDE_DETAILS"                   # answering the assistant's follow-up question
    CONFIRM = "CONFIRM"
    DECLINE = "DECLINE"
    OTHER = "OTHER"


class LeaveTypeCode(StrEnum):
    ANNUAL = "ANNUAL"
    SICK = "SICK"
    UNPAID = "UNPAID"
    BEREAVEMENT = "BEREAVEMENT"
    STUDY = "STUDY"
    PARENTAL = "PARENTAL"


class RequestStatusFilter(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    cancelled = "cancelled"


class IntentExtraction(BaseModel):
    intent: Intent
    leave_type: LeaveTypeCode | None = Field(None, description="Leave type the message refers to, if any")
    leave_type_explicit: bool = Field(False, description="True only if the user clearly named the type; False when "
                                                         "inferred from vague wording such as 'დასვენება'")
    start_date: str | None = Field(None, description="YYYY-MM-DD")
    end_date: str | None = Field(None, description="YYYY-MM-DD")
    reason: str | None = Field(None, description="Short reason the user gave (for unpaid leave), without health details")
    period_known_in_advance: bool | None = Field(None, description="Sick leave: user confirmed the future period is "
                                                                   "already known (doctor's note / planned surgery)")
    other_employee_id: str | None = Field(None, description="Another employee's ID if the user asks about someone else")
    year: int | None = None
    statuses: list[RequestStatusFilter] | None = None
    search_queries: list[str] = Field(default_factory=list,
                                      description="For policy questions: 1-2 rephrasings in formal Georgian policy "
                                                  "terminology (for document search)")


LEAVE_TYPE_GUIDE = """\
ANNUAL = ყოველწლიური ანაზღაურებადი შვებულება (also "შვებულება", "ანაზღაურებადი", "დასვენება" = probably ANNUAL but NOT explicit)
SICK = ავადმყოფობის შვებულება ("ავად ვარ", "ავადმყოფობის გამო")
UNPAID = უხელფასო შვებულება
BEREAVEMENT = გლოვის შვებულება (death of a relative: "გარდაიცვალა", "დაკრძალვა")
STUDY = სასწავლო/საგამოცდო შვებულება (exam, certification)
PARENTAL = მშობლის შვებულება (maternity, paternity, childcare, adoption: "დეკრეტი", "მამობის")"""

SYSTEM_PROMPT = """\
You classify messages sent to the Georgian-language HR assistant of Northstar Services and extract fields.
Return only the JSON object required by the schema. Never invent values the user did not give.

Intents:
- POLICY_QUESTION: asks what company rules/policies say (how many days, notice periods, probation, remote work,
  security, travel, learning, how to request something, etc.).
- BALANCE_QUERY: asks how many leave days they (or someone else) have left / used.
- CREATE_LEAVE_REQUEST: wants to take or request leave (even without dates or type).
- LIST_REQUESTS: wants to see their existing leave requests or their statuses.
- MODIFY_EXISTING_REQUEST: wants to cancel, change, withdraw, approve or reject an already submitted request.
- PROVIDE_DETAILS: answers the assistant's previous follow-up question (dates, type, reason, yes/no about details).
- CONFIRM / DECLINE: confirms or declines the request summary the assistant showed.
- OTHER: greetings, thanks, off-topic.

Leave types:
{leave_types}

Dates: today is {today} ({weekday}). Resolve relative dates ("ხვალ", "მომავალ ორშაბათს", "26-დან 30 ოქტომბრამდე")
to YYYY-MM-DD. Without a year use the nearest future date. A single day means start_date = end_date.
For unpaid leave copy the reason briefly; never ask for or copy health details.
"""


def build_system_prompt(today: date) -> str:
    weekdays = ["ორშაბათი", "სამშაბათი", "ოთხშაბათი", "ხუთშაბათი", "პარასკევი", "შაბათი", "კვირა"]
    return SYSTEM_PROMPT.format(leave_types=LEAVE_TYPE_GUIDE, today=today.isoformat(), weekday=weekdays[today.weekday()])


def build_user_prompt(message: str, state_summary: str) -> str:
    return f"Conversation state:\n{state_summary}\n\nUser message:\n{message}"
