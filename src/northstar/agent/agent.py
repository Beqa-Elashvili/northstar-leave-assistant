"""The Georgian HR assistant: intent routing, conversation state and the leave-request flow.

Routing:
- POLICY_QUESTION          → RAG (grounded answer + citations from metadata)
- BALANCE_QUERY            → MCP get_leave_balance
- LIST_REQUESTS            → MCP list_leave_requests
- CREATE_LEAVE_REQUEST     → slot filling → MCP propose_leave_request → explicit "დიახ" → MCP create_leave_request
- MODIFY_EXISTING_REQUEST  → explained refusal (assistant may not cancel/change/approve; policy 4.8, 12.3)
- BEREAVEMENT/STUDY/PARENTAL → policy explanation (RAG) + redirect; never proposed or created

The LLM only classifies and extracts. Dates are re-validated here; balances, notice, limits,
overlap, probation, restricted periods and authorization are decided by the MCP server.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from sqlalchemy.exc import SQLAlchemyError

from northstar.agent.formatting import (
    POLICY,
    format_balances,
    format_created,
    format_proposal,
    format_requests,
    format_violations,
    type_name,
)
from northstar.agent.intents import Intent, IntentExtraction, build_system_prompt, build_user_prompt
from northstar.agent.llm import LLMError, LLMProvider
from northstar.agent.policy_qa import PolicyAnswerer
from northstar.agent.tools import McpLeaveTools, ToolFailure
from northstar.rag.embeddings import EmbeddingError
from northstar.rag.retrieval import RagNotReady

logger = logging.getLogger("northstar.agent")

_YES = re.compile(r"^(დიახ|კი|ki|diax|diakh|yes|y|ok|ჰო|ho|დავადასტურებ|ვადასტურებ|ვეთანხმები|შექმენი)[.!\s]*$", re.I)
_NO = re.compile(r"^(არა|ara|no|n|არ მინდა|არ შექმნა|გაუქმება)[.!\s]*$", re.I)

ASSISTANT_TYPES = ("ANNUAL", "SICK", "UNPAID")

UNSUPPORTED_GUIDE = {
    "BEREAVEMENT": (
        "გლოვის შვებულება: რამდენი დღე ეკუთვნის ახლო ოჯახის წევრის და სხვა ნათესავის (ბებია, ბაბუა) "
        "გარდაცვალებისას, რა ვადაში გამოიყენება და როგორ წარდგება მოთხოვნა (მუხლები 8.1, 8.2, 8.3)?",
        "ამ მოთხოვნას ასისტენტი ვერ ქმნის. გლოვის შვებულების მოთხოვნა წარადგინეთ HR პორტალით ან ადამიანური "
        "რესურსების სამსახურის მეშვეობით, არაუგვიანეს 2 სამუშაო დღისა შვებულების პირველი დღიდან "
        f"({POLICY}, მუხლები 8.3 და 12.3).",
    ),
    "STUDY": (
        "სასწავლო და საგამოცდო შვებულება: რამდენი დღე ეკუთვნის, რა არის დამტკიცებული გეგმის მოთხოვნა და "
        "როგორ წარდგება (მუხლები 9.1, 9.2, 9.3)?",
        "ამ მოთხოვნას ასისტენტი ვერ ქმნის და ვერ ადასტურებს, არის თუ არა გამოცდა თქვენს დამტკიცებულ სწავლისა და "
        "განვითარების გეგმაში — ამას HR ამოწმებს. მოთხოვნა წარადგინეთ HR პორტალით ან HR-ის მეშვეობით, დაწყებამდე "
        f"სულ მცირე 10 სამუშაო დღით ადრე ({POLICY}, მუხლები 9.2, 9.3 და 12.3).",
    ),
    "PARENTAL": (
        "მშობლის შვებულება: რას მოიცავს და როგორ წარდგება მოთხოვნა (მუხლები 10.1, 10.2, 10.3, 10.4)?",
        "მშობლის შვებულება HR პორტალით ან ასისტენტით არ წარდგება. მიმართეთ პირდაპირ ადამიანური რესურსების "
        "სამსახურს, არაუგვიანეს 8 კვირით ადრე სავარაუდო დაწყებამდე — ხანგრძლივობას, ანაზღაურებასა და "
        f"დოკუმენტებს HR თქვენთან ერთად განსაზღვრავს ({POLICY}, მუხლები 10.2 და 12.3).",
    ),
}

MODIFY_REFUSAL = (
    "ასისტენტს არ შეუძლია უკვე წარდგენილი მოთხოვნის გაუქმება, შეცვლა, დამტკიცება ან უარყოფა.\n"
    "• განხილვის პროცესში მყოფი მოთხოვნის გაუქმება ნებისმიერ დროს შეგიძლიათ HR პორტალით.\n"
    "• დამტკიცებული შვებულების გაუქმება შესაძლებელია დაწყებამდე არაუგვიანეს 2 სამუშაო დღით ადრე; უფრო გვიან — "
    "უშუალო ხელმძღვანელის თანხმობით.\n"
    "• თარიღების ცვლილება ახალ მოთხოვნად ითვლება და მასზე წინასწარი შეტყობინების წესი ვრცელდება."
)
MODIFY_SOURCES = [f"{POLICY}, მუხლი 4.8 („გაუქმება და ცვლილება“)",
                  f"{POLICY}, მუხლი 12.3 („რისი გაკეთება არ შეუძლია HR ასისტენტს“)"]

HELP = (
    "შემიძლია დაგეხმაროთ:\n"
    "• კომპანიის პოლიტიკებზე კითხვებში (შვებულება, დისტანციური მუშაობა, უსაფრთხოება, სწავლა, მივლინება);\n"
    "• თქვენი შვებულების ბალანსისა და მოთხოვნების ნახვაში;\n"
    "• ყოველწლიური, ავადმყოფობის ან უხელფასო შვებულების მოთხოვნის შექმნაში.\n"
    "მაგალითად: „რამდენი დღე დამრჩა?“ ან „27 ოქტომბრიდან 30 ოქტომბრამდე შვებულება მინდა“."
)
ASK_TYPE = (
    "რა ტიპის შვებულება გსურთ?\n"
    "• ყოველწლიური ანაზღაურებადი\n• ავადმყოფობის\n• უხელფასო\n"
    "(გლოვის, სასწავლო და მშობლის შვებულების შემთხვევაში წესებს აგიხსნით და შესაბამის არხზე გადაგამისამართებთ.)"
)


class Awaiting(StrEnum):
    NOTHING = "nothing"
    LEAVE_TYPE = "leave_type"
    DATES = "dates"
    REASON = "reason"
    SICK_KNOWN = "sick_known_in_advance"
    CONFIRMATION = "confirmation"


@dataclass
class LeaveDraft:
    leave_type: str | None = None
    type_confirmed: bool = False
    start_date: date | None = None
    end_date: date | None = None
    reason: str | None = None
    period_known_in_advance: bool = False


@dataclass
class ConversationState:
    conversation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    employee_id: str | None = None
    employee_name: str | None = None
    draft: LeaveDraft | None = None
    awaiting: Awaiting = Awaiting.NOTHING
    proposal_id: str | None = None
    last_created_proposal_id: str | None = None
    last_reply: str = ""

    def summary(self) -> str:
        d = self.draft
        draft = "none" if d is None else (
            f"type={d.leave_type} (confirmed={d.type_confirmed}), start={d.start_date}, end={d.end_date}, "
            f"reason={'given' if d.reason else 'none'}")
        return (f"employee={self.employee_id}; awaiting={self.awaiting.value}; leave draft: {draft}; "
                f"proposal shown={'yes' if self.proposal_id else 'no'}\n"
                f"assistant's last message: {self.last_reply[:400]}")


@dataclass
class AgentReply:
    text: str
    sources: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    streamed: bool = False


class HRAgent:
    def __init__(self, tools: McpLeaveTools, llm: LLMProvider, answerer: PolicyAnswerer, today: date):
        self.tools = tools
        self.llm = llm
        self.answerer = answerer
        self.today = today
        self.state = ConversationState()
        self.leave_types: dict[str, dict] = {}

    async def start(self) -> dict:
        profile = await self.tools.profile()
        self.state.employee_id, self.state.employee_name = profile["employee_id"], profile["full_name"]
        self.leave_types = {t["code"]: t for t in await self.tools.leave_types()}
        self.tools.calls.clear()
        return profile

    def reset(self) -> None:
        """Start a new conversation (new conversation_id); any shown proposal is simply left to expire."""
        self.state = ConversationState(employee_id=self.state.employee_id, employee_name=self.state.employee_name)

    # --- entry point ------------------------------------------------------------------------------

    async def handle(self, message: str, on_chunk: Callable[[str], None] | None = None) -> AgentReply:
        first_call = len(self.tools.calls)
        try:
            reply = await self._handle(message.strip(), on_chunk)
        except ToolFailure as exc:
            reply = AgentReply(exc.message + (f" ({POLICY}, მუხლი {exc.article})" if exc.article else ""))
        except LLMError:
            logger.warning("LLM unavailable", exc_info=True)
            reply = AgentReply("ბოდიში, AI სერვისი ამ წუთას მიუწვდომელია და შეტყობინების დამუშავება ვერ მოხერხდა. "
                               "სცადეთ ცოტა ხანში. (ბალანსისა და მოთხოვნების მონაცემები უსაფრთხოდაა.)")
        except RagNotReady:
            reply = AgentReply("პოლიტიკის დოკუმენტების საძიებო ბაზა მზად არ არის. ადმინისტრატორმა უნდა გაუშვას: "
                               "python -m scripts.ingest_documents")
        except EmbeddingError:
            logger.warning("embedding service unavailable", exc_info=True)
            reply = AgentReply("პოლიტიკის დოკუმენტებში ძიება ამ წუთას მიუწვდომელია. სცადეთ ცოტა ხანში.")
        except SQLAlchemyError:
            logger.warning("document store unavailable", exc_info=True)
            reply = AgentReply("დოკუმენტების ბაზასთან კავშირი ვერ მოხერხდა. სცადეთ ცოტა ხანში.")
        except Exception:
            logger.exception("unexpected agent error")
            reply = AgentReply("ბოდიში, მოულოდნელი შეცდომა მოხდა. სცადეთ თავიდან.")
        reply.tools = [c.name for c in self.tools.calls[first_call:]]
        self.state.last_reply = reply.text
        return reply

    async def _handle(self, message: str, on_chunk) -> AgentReply:
        if not message:
            return AgentReply(HELP)
        s = self.state

        # Short yes/no answers to the assistant's own question are handled without the LLM.
        if _YES.match(message) or _NO.match(message):
            yes = bool(_YES.match(message))
            if s.awaiting == Awaiting.CONFIRMATION and s.proposal_id:
                return await (self._confirm() if yes else self._decline())
            if s.awaiting == Awaiting.LEAVE_TYPE:
                if yes:  # "yes" is not a leave type
                    return self._ask(Awaiting.LEAVE_TYPE, ASK_TYPE)
                self._reset_draft()
                return AgentReply("კარგი. სხვა რით შემიძლია დაგეხმაროთ?")
            if s.awaiting == Awaiting.SICK_KNOWN and s.draft:
                if yes:
                    s.draft.period_known_in_advance = True
                    return await self._advance()
                self._reset_draft()
                return AgentReply("გასაგებია. მომავალი თარიღით ავადმყოფობის შვებულება აღირიცხება მხოლოდ მაშინ, როცა "
                                  "პერიოდი წინასწარ არის ცნობილი. ავადმყოფობისას აღრიცხეთ ის პირველ დღეს "
                                  f"({POLICY}, მუხლი 6.2).")
            if yes and s.awaiting == Awaiting.NOTHING and s.last_created_proposal_id:
                # Repeated confirmation: the server returns the existing request (idempotent).
                return AgentReply(format_created(await self.tools.create(s.last_created_proposal_id)))

        extraction = await self.llm.generate_structured(
            build_system_prompt(self.today), build_user_prompt(message, s.summary()), IntentExtraction)
        return await self._route(message, extraction, on_chunk)

    async def _route(self, message: str, x: IntentExtraction, on_chunk) -> AgentReply:
        s = self.state
        intent = x.intent

        if intent == Intent.CONFIRM:
            if s.awaiting == Awaiting.CONFIRMATION and s.proposal_id:
                return await self._confirm()
            if s.last_created_proposal_id:
                return AgentReply(format_created(await self.tools.create(s.last_created_proposal_id)))
            return AgentReply("დასადასტურებელი მოთხოვნა ამ წუთას არ არის. " + HELP)
        if intent == Intent.DECLINE:
            if s.awaiting == Awaiting.CONFIRMATION and s.proposal_id:
                return await self._decline()
            self._reset_draft()
            return AgentReply("კარგი. სხვა რით შემიძლია დაგეხმაროთ?")

        if intent == Intent.CREATE_LEAVE_REQUEST or (intent == Intent.PROVIDE_DETAILS and s.draft is not None):
            return await self._update_draft(x, new_request=intent == Intent.CREATE_LEAVE_REQUEST)
        if intent == Intent.BALANCE_QUERY:
            data = await self.tools.balance(year=x.year, leave_type=x.leave_type.value if x.leave_type else None,
                                            employee_id=x.other_employee_id)
            return AgentReply(format_balances(data))
        if intent == Intent.LIST_REQUESTS:
            data = await self.tools.requests(statuses=[st.value for st in x.statuses] if x.statuses else None,
                                             leave_type=x.leave_type.value if x.leave_type else None,
                                             employee_id=x.other_employee_id)
            return AgentReply(format_requests(data))
        if intent == Intent.MODIFY_EXISTING_REQUEST:
            return AgentReply(MODIFY_REFUSAL, MODIFY_SOURCES)
        if intent == Intent.POLICY_QUESTION:
            answer = await self.answerer.answer(message, rephrasings=x.search_queries, on_chunk=on_chunk)
            return AgentReply(answer.text, answer.sources, streamed=on_chunk is not None and answer.found)
        if intent == Intent.PROVIDE_DETAILS:
            return AgentReply("ბოდიში, ვერ გავიგე, რას გულისხმობთ. " + HELP)
        return AgentReply(HELP)

    # --- leave request flow -----------------------------------------------------------------------

    def _ask(self, awaiting: Awaiting, text: str) -> AgentReply:
        self.state.awaiting = awaiting
        return AgentReply(text)

    def _reset_draft(self) -> None:
        self.state.draft, self.state.proposal_id, self.state.awaiting = None, None, Awaiting.NOTHING

    async def _update_draft(self, x: IntentExtraction, *, new_request: bool) -> AgentReply:
        s = self.state
        new_type = x.leave_type.value if x.leave_type else None
        if s.draft is None or (new_request and new_type and new_type != s.draft.leave_type):
            s.draft = LeaveDraft()
        if s.proposal_id:  # any change invalidates the summary that was shown
            s.proposal_id = None
        d = s.draft

        try:
            start = date.fromisoformat(x.start_date) if x.start_date else None
            end = date.fromisoformat(x.end_date) if x.end_date else None
        except ValueError:
            return self._ask(Awaiting.DATES, "თარიღი ვერ გავიგე. მიუთითეთ, მაგალითად: 2026-11-10 – 2026-11-13.")
        if start:
            d.start_date, d.end_date = start, end or start
        elif end and d.start_date:
            d.end_date = end

        if new_type:
            d.leave_type = new_type
            # "შვებულება" alone is ambiguous; with dates in the same message it is read as annual leave
            # (the type is shown again in the confirmation summary).
            d.type_confirmed = x.leave_type_explicit or new_type != "ANNUAL" or start is not None
        if x.reason:
            d.reason = x.reason.strip()
        if x.period_known_in_advance:
            d.period_known_in_advance = True
        return await self._advance()

    async def _advance(self) -> AgentReply:
        s = self.state
        d = s.draft
        if d is None or d.leave_type is None:
            return self._ask(Awaiting.LEAVE_TYPE, ASK_TYPE)
        info = self.leave_types.get(d.leave_type, {})
        if not info.get("assistant_supported", False):
            return await self._explain_unsupported(d.leave_type)
        if not d.type_confirmed:  # a guess from vague wording ("შვებულება", "დასვენება") is not a choice
            d.leave_type = None
            return self._ask(Awaiting.LEAVE_TYPE, ASK_TYPE)
        if d.leave_type == "UNPAID" and not d.reason:
            return self._ask(Awaiting.REASON, "მოკლედ გთხოვთ მიუთითოთ მიზეზი (ჯანმრთელობის დეტალების გარეშე).")
        if d.start_date is None:
            return self._ask(Awaiting.DATES, f"რომელი თარიღებით გსურთ {type_name(d.leave_type).lower()}? "
                                             "(მაგ. 10 ნოემბრიდან 13 ნოემბრამდე)")
        if d.end_date < d.start_date:
            d.start_date = d.end_date = None
            return self._ask(Awaiting.DATES, "დასრულების თარიღი დაწყების თარიღზე ადრეა. გთხოვთ, თავიდან მიუთითოთ "
                                             "თარიღები.")

        p = await self.tools.propose(
            conversation_id=s.conversation_id, leave_type=d.leave_type, start_date=d.start_date,
            end_date=d.end_date, comment=d.reason, sick_period_known_in_advance=d.period_known_in_advance)
        if p["outcome"] == "awaiting_confirmation":
            s.proposal_id = p["proposal_id"]
            return self._ask(Awaiting.CONFIRMATION, format_proposal(p))
        codes = {v["code"] for v in p["violations"]}
        messages = "\n".join(f"{v['message']} ({POLICY}, მუხლი {v['article']})" for v in p["violations"])
        if p["outcome"] == "needs_input":
            if codes & {"reason_required", "health_details_in_reason", "reason_too_long"}:
                d.reason = None
                return self._ask(Awaiting.REASON, messages)
            if "sick_future_confirmation" in codes:
                return self._ask(Awaiting.SICK_KNOWN, messages + "\n\nპერიოდი წინასწარ არის ცნობილი? (დიახ/არა)")
        text = format_violations(p)
        if all(v["kind"] == "rejected" for v in p["violations"]):
            d.start_date = d.end_date = None
            return self._ask(Awaiting.DATES, text + "\n\nშეგიძლიათ მიუთითოთ სხვა თარიღები.")
        self._reset_draft()
        return AgentReply(text)

    async def _explain_unsupported(self, code: str) -> AgentReply:
        question, redirect = UNSUPPORTED_GUIDE[code]
        self._reset_draft()
        prefix = "გულწრფელად გიზიარებთ მწუხარებას.\n\n" if code == "BEREAVEMENT" else ""
        try:
            answer = await self.answerer.answer(
                question, topic="leave",
                instruction="მოკლედ ახსენი მხოლოდ ამ შვებულების წესები. პირადი ან ჯანმრთელობის დეტალებს ნუ ითხოვ. "
                            "არ თქვა, რომ მოთხოვნას შენ შექმნი.")
        except (LLMError, RagNotReady, EmbeddingError, SQLAlchemyError):
            logger.warning("policy explanation unavailable", exc_info=True)
            return AgentReply(f"{prefix}{redirect}")
        body = f"{answer.text}\n\n" if answer.found else ""
        return AgentReply(f"{prefix}{body}{redirect}", answer.sources)

    async def _confirm(self) -> AgentReply:
        s = self.state
        proposal_id = s.proposal_id
        try:
            result = await self.tools.create(proposal_id)
        except ToolFailure:
            self._reset_draft()  # the shown summary is no longer valid; never retry it implicitly
            raise
        s.last_created_proposal_id = proposal_id
        self._reset_draft()
        return AgentReply(format_created(result))

    async def _decline(self) -> AgentReply:
        await self.tools.decline(self.state.proposal_id)
        self._reset_draft()
        return AgentReply("კარგი, მოთხოვნა არ შეიქმნა.")
