"""Agent routing, conversation state and edge cases (LLM mocked; MCP server, DB and RAG are real).

The 10 assignment scenarios (section 48) are in test_scenarios.py.
"""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from northstar.agent.policy_qa import NO_INFO
from northstar.agent.tools import McpLeaveTools, ToolFailure
from northstar.database.models import LeaveProposal, LeaveRequest
from tests.agent_helpers import X, BrokenLLM, FakeLLM, converse, ingest_test_documents

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def retriever(engine):
    return ingest_test_documents(engine)


@pytest.fixture()
def talk(seeded_engine, retriever):
    def run(llm, *messages, employee="E1001"):
        return converse(seeded_engine, retriever, llm, list(messages), employee)
    return run


def requests_in_db(engine):
    with Session(engine) as s:
        return list(s.scalars(select(LeaveRequest).order_by(LeaveRequest.request_id)))


# --- routing -------------------------------------------------------------------------------------

def test_policy_question_uses_rag_with_sources(talk):
    q = "რამდენი სამუშაო დღით ადრე უნდა წარვადგინო ყოველწლიური შვებულების მოთხოვნა?"
    llm = FakeLLM({q: X("POLICY_QUESTION", search_queries=["მოთხოვნის წარდგენა და წინასწარი შეტყობინება"])})
    (reply,), _ = talk(llm, q)
    assert reply.tools == []                                          # no MCP call for policy questions
    assert reply.sources and reply.sources[0].startswith("[1] შვებულებისა და გაცდენის პოლიტიკა v4.0")
    assert "წყარო: " in llm.text_prompts[0] and "სტატუსი: " in llm.text_prompts[0]


def test_grouped_citations_are_resolved(talk):
    q = "რამდენი სამუშაო დღით ადრე უნდა წარვადგინო ყოველწლიური შვებულების მოთხოვნა?"
    llm = FakeLLM({q: X("POLICY_QUESTION")}, answer="ორი ფაქტი [1, 2] და კიდევ ერთი [3,1].")
    (reply,), _ = talk(llm, q)
    assert [s.split("]")[0] for s in reply.sources] == ["[1", "[2", "[3"]


def test_policy_question_without_information(talk):
    q = "როგორ მოვამზადო ხაჭაპური?"
    llm = FakeLLM({q: X("POLICY_QUESTION")})
    (reply,), _ = talk(llm, q)
    assert reply.text == NO_INFO and reply.sources == [] and llm.text_prompts == []  # LLM not asked to guess


def test_all_balances_and_sick_note(talk):
    q = "რამდენი დღე დამრჩა?"
    (reply,), _ = talk(FakeLLM({q: X("BALANCE_QUERY")}), q)
    assert "ავადმყოფობის შვებულება" in reply.text and "(ანაზღაურებადი)" in reply.text and "მუხლი 6.4" in reply.text


def test_list_requests(talk):
    q = "ჩემი მოთხოვნები მაჩვენე"
    (reply,), _ = talk(FakeLLM({q: X("LIST_REQUESTS")}), q)
    assert reply.tools == ["list_leave_requests"]
    assert "#5" in reply.text and "განხილვის პროცესში" in reply.text and "#3" in reply.text


def test_other_intent_shows_help(talk):
    (reply,), _ = talk(FakeLLM({"გამარჯობა": X("OTHER")}), "გამარჯობა")
    assert "შემიძლია დაგეხმაროთ" in reply.text and reply.tools == []


# --- Scenario 2: annual request --------------------------------------------------------------------


def test_nothing_created_before_confirmation(talk, seeded_engine):
    m = "27-დან 30 ოქტომბრამდე ყოველწლიური შვებულება მინდა"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=True,
                        start_date="2026-10-27", end_date="2026-10-30")})
    talk(llm, m)
    assert len(requests_in_db(seeded_engine)) == 27


def test_ambiguous_request_asks_type_then_keeps_state(talk, seeded_engine):
    m1, m2, m3 = "შვებულება მინდა.", "ანაზღაურებადი.", "27-დან 30 ოქტომბრამდე."
    llm = FakeLLM({
        m1: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=False),
        m2: X("PROVIDE_DETAILS", leave_type="ANNUAL", leave_type_explicit=True),
        m3: X("PROVIDE_DETAILS", start_date="2026-10-27", end_date="2026-10-30"),
    })
    (r1, r2, r3), agent = talk(llm, m1, m2, m3)
    assert "რა ტიპის შვებულება გსურთ?" in r1.text and r1.tools == []
    assert "რომელი თარიღებით" in r2.text
    assert "დღეების რაოდენობა: 4 სამუშაო დღე" in r3.text   # type remembered, not asked again
    assert agent.state.awaiting == "confirmation"


def test_vague_rest_wording_is_clarified(talk):
    m = "დასვენება მინდა."
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=False),
                   "ყოველწლიური": X("PROVIDE_DETAILS", leave_type="ANNUAL", leave_type_explicit=True)})
    (r1, r2, r3), agent = talk(llm, m, "დიახ", "ყოველწლიური")
    assert r1.text.startswith("რა ტიპის შვებულება გსურთ?") and "(დიახ/არა)" not in r1.text
    assert r2.text.startswith("რა ტიპის შვებულება გსურთ?")   # "yes" is not a type: ask again
    assert "რომელი თარიღებით" in r3.text and agent.state.draft.leave_type == "ANNUAL"


# --- Scenario 3: unpaid ----------------------------------------------------------------------------


def test_unpaid_notice_violation(talk):
    m = "ხვალიდან უხელფასო შვებულება მინდა, პირადი მიზეზით"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="UNPAID", leave_type_explicit=True,
                        start_date="2026-10-20", end_date="2026-10-21", reason="პირადი მიზეზი")})
    (r,), _ = talk(llm, m)
    assert "10 სამუშაო დღით" in r.text and "მუხლი 7.2" in r.text


def test_unpaid_health_reason_is_not_accepted(talk):
    m = "უხელფასო მინდა 3 ნოემბრიდან, ოპერაცია მაქვს"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="UNPAID", leave_type_explicit=True,
                        start_date="2026-11-03", end_date="2026-11-03", reason="ოპერაცია მაქვს")})
    (r,), agent = talk(llm, m)
    assert "ჯანმრთელობის დეტალებს ნუ მიუთითებთ" in r.text and agent.state.awaiting == "reason"


# --- Scenario 4: sick ------------------------------------------------------------------------------


def test_sick_request_is_created(talk, seeded_engine):
    m = "ავად ვარ, დღეს და ხვალ ვერ მოვალ"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="SICK", leave_type_explicit=True,
                        start_date="2026-10-19", end_date="2026-10-20")})
    (r1, r2), _ = talk(llm, m, "კი")
    assert "2 სამუშაო დღე" in r1.text and "#28" in r2.text
    assert requests_in_db(seeded_engine)[-1].leave_type == "SICK"


def test_future_sick_leave_asks_if_period_known(talk):
    m = "2 ნოემბრიდან 4 ნოემბრამდე ავადმყოფობის შვებულება მჭირდება"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="SICK", leave_type_explicit=True,
                        start_date="2026-11-02", end_date="2026-11-04")})
    (r1, r2), _ = talk(llm, m, "დიახ")
    assert "წინასწარ არის ცნობილი? (დიახ/არა)" in r1.text
    assert "შევქმნა მოთხოვნა?" in r2.text


# --- Scenarios 5–7: unsupported types --------------------------------------------------------------


def test_unsupported_type_still_redirects_when_llm_fails_for_answer(talk):
    class AnswerFails(FakeLLM):
        async def generate_text(self, system, prompt, on_chunk=None):
            from northstar.agent.llm import LLMError
            raise LLMError("down")

    m = "მამობის შვებულება მინდა"
    (r,), _ = talk(AnswerFails({m: X("CREATE_LEAVE_REQUEST", leave_type="PARENTAL", leave_type_explicit=True)}), m)
    assert "ადამიანური რესურსების სამსახურს" in r.text


# --- Scenario 9 & 10, decline, other restrictions ----------------------------------------------------


def test_decline_creates_nothing(talk, seeded_engine):
    m = "27-დან 30 ოქტომბრამდე ყოველწლიური შვებულება მინდა"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=True,
                        start_date="2026-10-27", end_date="2026-10-30")})
    (_, r), _ = talk(llm, m, "არა")
    assert r.text == "კარგი, მოთხოვნა არ შეიქმნა." and r.tools == ["decline_leave_proposal"]
    assert len(requests_in_db(seeded_engine)) == 27


def test_audit_restricted_period_redirects_to_manager(talk):
    m = "14-დან 18 დეკემბრამდე ყოველწლიური შვებულება მინდა"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=True,
                        start_date="2026-12-14", end_date="2026-12-18")})
    (r,), agent = talk(llm, m)
    assert "მუხლი 4.6" in r.text and "უშუალო ხელმძღვანელი" in r.text and agent.state.draft is None


def test_probation_employee(talk):
    m = "24-დან 26 ნოემბრამდე ყოველწლიური შვებულება მინდა"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=True,
                        start_date="2026-11-24", end_date="2026-11-26")})
    (r,), _ = talk(llm, m, employee="E1004")
    assert "გამოსაცდელი ვადის" in r.text and "მუხლი 4.3" in r.text


def test_invalid_date_from_llm_is_not_trusted(talk):
    m = "31 ნოემბრიდან შვებულება მინდა"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=True,
                        start_date="2026-11-31")})
    (r,), agent = talk(llm, m)
    assert "თარიღი ვერ გავიგე" in r.text and r.tools == [] and agent.state.awaiting == "dates"


def test_llm_failure_is_reported_in_georgian(talk):
    (r,), _ = talk(BrokenLLM(), "რამდენი დღე დამრჩა?")
    assert "AI სერვისი ამ წუთას მიუწვდომელია" in r.text and r.tools == []


def test_tool_allow_list_excludes_hr_actions():
    import anyio

    tools = McpLeaveTools(client=None)  # never reaches the client

    async def go():
        with pytest.raises(ToolFailure) as exc:
            await tools._call("approve_leave_request", request_id=5)
        return exc.value.code

    assert anyio.run(go) == "tool_not_allowed"


# --- Phase 11: confirmation hardening at the agent level -----------------------------------------

ANNUAL_27_30 = "27-დან 30 ოქტომბრამდე ყოველწლიური შვებულება მინდა"


def annual_llm():
    return FakeLLM({ANNUAL_27_30: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=True,
                                    start_date="2026-10-27", end_date="2026-10-30")})


def test_create_sends_exactly_the_shown_proposal(talk):
    (shown, _), agent = talk(annual_llm(), ANNUAL_27_30, "დიახ")
    create = [c for c in agent.tools.calls if c.name == "create_leave_request"][0]
    assert {k: create.arguments[k] for k in ("leave_type", "start_date", "end_date")} == \
        {"leave_type": "ANNUAL", "start_date": "2026-10-27", "end_date": "2026-10-30"}
    assert "employee_id" not in create.arguments and "2026-10-27 – 2026-10-30" in shown.text


def test_expired_summary_is_rechecked_and_shown_again(talk, seeded_engine):
    def expire():
        with seeded_engine.begin() as conn:
            conn.execute(text("UPDATE leave_proposals SET created_at = created_at - interval '2 hours', "
                              "expires_at = created_at - interval '1 hour'"))

    (shown, again, created), agent = talk(annual_llm(), ANNUAL_27_30, expire, "დიახ", "დიახ")
    assert again.tools == ["create_leave_request", "propose_leave_request"]   # refused, then re-checked
    assert again.text.startswith("წინა შეჯამებას ვადა გაუვიდა") and "შევქმნა მოთხოვნა?" in again.text
    assert "#28" in created.text
    assistant_rows = [r for r in requests_in_db(seeded_engine) if r.created_via == "assistant"]
    assert len(assistant_rows) == 1                                  # only the second, fresh "yes" created it
    assert str(assistant_rows[0].proposal_id) == agent.state.last_created_proposal["proposal_id"]


def test_rules_changed_before_yes_creates_nothing(talk, seeded_engine):
    def book_overlapping_in_portal():
        with seeded_engine.begin() as conn:
            conn.execute(text("INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, days, "
                              "status, created_at, created_via) VALUES ('E1001','ANNUAL','2026-10-29','2026-10-29',"
                              "1,'pending', now(), 'portal')"))

    (_, r, again), agent = talk(annual_llm(), ANNUAL_27_30, book_overlapping_in_portal, "დიახ", "დიახ")
    assert "პირობები შეიცვალა" in r.text and "მოთხოვნა არ შეიქმნა" in r.text and "•" in r.text
    assert agent.state.proposal is None and agent.state.awaiting == "nothing"
    assert "create_leave_request" not in again.tools                # a later "yes" never re-creates implicitly
    assert all(req.created_via != "assistant" for req in requests_in_db(seeded_engine))


@pytest.mark.parametrize("leave_type, days, expected, absent", [
    ("ANNUAL", 4, "მუხლი 4.4", "მუხლი 7.3"),
    ("UNPAID", 4, "მუხლი 7.3", "მუხლი 4.4"),
    ("SICK", 3, "მუხლი 6.3", "მუხლი 4.4"),
    ("SICK", 2, "მუხლი 12.2", "მუხლი 6.3"),
])
def test_created_message_states_only_that_types_policy(leave_type, days, expected, absent):
    from northstar.agent.formatting import format_created

    text = format_created({"request": {"request_id": 28, "leave_type": leave_type, "start_date": "2026-11-03",
                                       "end_date": "2026-11-06", "days": days, "status": "pending"}})
    assert "#28" in text and "დამტკიცებას არ ნიშნავს (მუხლი 12.2)" in text
    assert expected in text and absent not in text
