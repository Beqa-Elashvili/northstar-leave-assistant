"""Agent routing and the 10 assignment scenarios (LLM mocked; MCP server, DB and RAG are real)."""

import pytest
from sqlalchemy import func, select
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


def test_policy_question_without_information(talk):
    q = "როგორ მოვამზადო ხაჭაპური?"
    llm = FakeLLM({q: X("POLICY_QUESTION")})
    (reply,), _ = talk(llm, q)
    assert reply.text == NO_INFO and reply.sources == [] and llm.text_prompts == []  # LLM not asked to guess


def test_scenario_1_balance_uses_mcp(talk):
    q = "რამდენი ANNUAL დღე დამრჩა?"
    (reply,), _ = talk(FakeLLM({q: X("BALANCE_QUERY", leave_type="ANNUAL")}), q)
    assert reply.tools == ["get_leave_balance"]
    assert "(2026 წელი)" in reply.text
    for fragment in ("კუთვნილი: 25 + გადმოტანილი: 3", "დამტკიცებული: 15", "განხილვის პროცესში: 3",
                     "ხელმისაწვდომი: 10 სამუშაო დღე"):
        assert fragment in reply.text


def test_all_balances_and_sick_note(talk):
    q = "რამდენი დღე დამრჩა?"
    (reply,), _ = talk(FakeLLM({q: X("BALANCE_QUERY")}), q)
    assert "ავადმყოფობის შვებულება" in reply.text and "(ანაზღაურებადი)" in reply.text and "მუხლი 6.4" in reply.text


def test_scenario_8_other_employee_balance_rejected(talk):
    q = "მაჩვენე E1002-ის ბალანსი."
    (reply,), _ = talk(FakeLLM({q: X("BALANCE_QUERY", other_employee_id="E1002")}), q)
    assert reply.tools == ["get_leave_balance"]                      # the server decided, not the prompt
    assert "სხვა თანამშრომლის მონაცემების ნახვა შეუძლებელია" in reply.text
    assert "ხელმისაწვდომი" not in reply.text and "24" not in reply.text


def test_list_requests(talk):
    q = "ჩემი მოთხოვნები მაჩვენე"
    (reply,), _ = talk(FakeLLM({q: X("LIST_REQUESTS")}), q)
    assert reply.tools == ["list_leave_requests"]
    assert "#5" in reply.text and "განხილვის პროცესში" in reply.text and "#3" in reply.text


def test_other_intent_shows_help(talk):
    (reply,), _ = talk(FakeLLM({"გამარჯობა": X("OTHER")}), "გამარჯობა")
    assert "შემიძლია დაგეხმაროთ" in reply.text and reply.tools == []


# --- Scenario 2: annual request --------------------------------------------------------------------

def test_scenario_2_annual_flow(talk, seeded_engine):
    m1 = "26 ოქტომბრიდან 30 ოქტომბრამდე შვებულება მინდა."
    m2 = "მაშინ 27-დან 30 ოქტომბრამდე."
    llm = FakeLLM({
        m1: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", start_date="2026-10-26", end_date="2026-10-30"),
        m2: X("PROVIDE_DETAILS", start_date="2026-10-27", end_date="2026-10-30"),
    })
    (r1, r2, r3), _ = talk(llm, m1, m2, "დიახ")
    # 26 Oct: notice period violated (only 4 working days); explained with article and alternative.
    assert r1.tools == ["propose_leave_request"]
    assert "მუხლი 4.4" in r1.text and "2026-10-27" in r1.text and "სხვა თარიღები" in r1.text
    # 27–30 Oct: proposal with type, period, days, then explicit confirmation question.
    assert r2.tools == ["propose_leave_request"]
    for fragment in ("ტიპი: ყოველწლიური ანაზღაურებადი შვებულება", "პერიოდი: 2026-10-27 – 2026-10-30",
                     "დღეების რაოდენობა: 4 სამუშაო დღე", "შევქმნა მოთხოვნა? (დიახ/არა)"):
        assert fragment in r2.text
    # "დიახ" is handled deterministically (no LLM) and creates exactly one pending request.
    assert r3.tools == ["create_leave_request"] and "#28" in r3.text and "განხილვის პროცესში" in r3.text
    created = requests_in_db(seeded_engine)[-1]
    assert (created.request_id, created.status, created.created_via, created.days) == (28, "pending", "assistant", 4)
    assert llm.structured_calls == 2


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
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=False)})
    (r1, r2), _ = talk(llm, m, "დიახ")
    assert "ყოველწლიური ანაზღაურებადი შვებულება გსურთ? (დიახ/არა)" in r1.text
    assert "რომელი თარიღებით" in r2.text


# --- Scenario 3: unpaid ----------------------------------------------------------------------------

def test_scenario_3_unpaid_flow(talk, seeded_engine):
    m1, m2, m3 = "უხელფასო შვებულება მინდა.", "ოჯახური მიზეზი", "3-დან 6 ნოემბრამდე"
    llm = FakeLLM({
        m1: X("CREATE_LEAVE_REQUEST", leave_type="UNPAID", leave_type_explicit=True),
        m2: X("PROVIDE_DETAILS", reason="ოჯახური მიზეზი"),
        m3: X("PROVIDE_DETAILS", start_date="2026-11-03", end_date="2026-11-06"),
    })
    (r1, r2, r3, r4), _ = talk(llm, m1, m2, m3, "დიახ")
    assert r1.text == "მოკლედ გთხოვთ მიუთითოთ მიზეზი (ჯანმრთელობის დეტალების გარეშე)."
    assert "რომელი თარიღებით" in r2.text
    assert "4 კალენდარული დღე" in r3.text and "მიზეზი: ოჯახური მიზეზი" in r3.text
    assert "#28" in r4.text
    created = requests_in_db(seeded_engine)[-1]
    assert (created.leave_type, created.comment, created.days) == ("UNPAID", "ოჯახური მიზეზი", 4)


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

def test_scenario_4_sick_over_paid_balance(talk, seeded_engine):
    m = "ავადმყოფობის გამო 19-დან 29 ოქტომბრამდე ვერ ვიმუშავებ"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="SICK", leave_type_explicit=True,
                        start_date="2026-10-19", end_date="2026-10-29")})
    (r,), agent = talk(llm, m)
    assert "create_leave_request" not in r.tools
    assert "აღემატება თქვენს დარჩენილ ანაზღაურებად ავადმყოფობის დღეებს (8)" in r.text
    assert "არ ნიშნავს, რომ ავადმყოფობის აღრიცხვა აღარ შეიძლება" in r.text
    assert "ადამიანური რესურსების სამსახური" in r.text and "მუხლი 6.4" in r.text
    assert len(requests_in_db(seeded_engine)) == 27 and agent.state.draft is None


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

def test_scenario_5_bereavement(talk, seeded_engine):
    m = "ბებიაჩემი გარდაიცვალა და შვებულება მინდა."
    (r,), agent = talk(FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="BEREAVEMENT", leave_type_explicit=True)}), m)
    assert r.tools == []                                              # never proposed or created
    assert r.text.startswith("გულწრფელად გიზიარებთ მწუხარებას.")
    assert "ასისტენტი ვერ ქმნის" in r.text and "HR პორტალით" in r.text and "მუხლები 8.3 და 12.3" in r.text
    assert any("შვებულებისა და გაცდენის პოლიტიკა" in s for s in r.sources)
    assert len(requests_in_db(seeded_engine)) == 27 and agent.state.draft is None


def test_scenario_6_study(talk, seeded_engine):
    m = "ACCA-ს გამოცდისთვის შვებულება მინდა 16 ნოემბერს"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="STUDY", leave_type_explicit=True,
                        start_date="2026-11-16", end_date="2026-11-16")})
    (r,), _ = talk(llm, m)
    assert r.tools == []
    assert "HR ამოწმებს" in r.text and "10 სამუშაო დღით ადრე" in r.text
    with Session(seeded_engine) as s:
        assert s.scalar(select(func.count()).select_from(LeaveProposal)) == 0


def test_scenario_7_parental(talk):
    m = "მამობის შვებულება მინდა"
    (r,), _ = talk(FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="PARENTAL", leave_type_explicit=True)}), m)
    assert r.tools == [] and "პირდაპირ ადამიანური რესურსების სამსახურს" in r.text and "8 კვირით" in r.text


def test_unsupported_type_still_redirects_when_llm_fails_for_answer(talk):
    class AnswerFails(FakeLLM):
        async def generate_text(self, system, prompt, on_chunk=None):
            from northstar.agent.llm import LLMError
            raise LLMError("down")

    m = "მამობის შვებულება მინდა"
    (r,), _ = talk(AnswerFails({m: X("CREATE_LEAVE_REQUEST", leave_type="PARENTAL", leave_type_explicit=True)}), m)
    assert "ადამიანური რესურსების სამსახურს" in r.text


# --- Scenario 9 & 10, decline, other restrictions ----------------------------------------------------

def test_scenario_9_cancel_is_refused_without_calling_tools(talk, seeded_engine):
    m = "ჩემი მოთხოვნა გააუქმე."
    (r,), _ = talk(FakeLLM({m: X("MODIFY_EXISTING_REQUEST")}), m)
    assert r.tools == []
    assert "არ შეუძლია უკვე წარდგენილი მოთხოვნის გაუქმება" in r.text and "HR პორტალით" in r.text
    assert any("მუხლი 4.8" in s for s in r.sources) and any("მუხლი 12.3" in s for s in r.sources)
    assert requests_in_db(seeded_engine)[4].status == "pending"   # request #5 untouched


def test_scenario_10_duplicate_confirmation(talk, seeded_engine):
    m = "27-დან 30 ოქტომბრამდე ყოველწლიური შვებულება მინდა"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=True,
                        start_date="2026-10-27", end_date="2026-10-30")})
    (_, first, second), _ = talk(llm, m, "დიახ", "დიახ")
    assert "#28" in first.text and "მოთხოვნა შეიქმნა" in first.text
    assert second.tools == ["create_leave_request"]                # same proposal sent again ...
    assert "უკვე შექმნილია" in second.text and "#28" in second.text  # ... server returns the original
    assert len(requests_in_db(seeded_engine)) == 28


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
