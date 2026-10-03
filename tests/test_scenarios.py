"""The 10 scenarios of assignment section 48, end to end, with the assignment's own wording.

Each test asserts the "Expected" bullets of its scenario. The LLM is mocked (it only classifies and
extracts); the MCP server, PostgreSQL data and RAG retrieval are real.
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from northstar.agent.llm import LLMError
from northstar.database.models import LeaveProposal, LeaveRequest
from tests.agent_helpers import X, FakeLLM, converse, ingest_test_documents

pytestmark = pytest.mark.db

SEEDED_REQUESTS = 27


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


def proposals_in_db(engine) -> int:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(LeaveProposal))


# --- Scenario 1 — Balance ---------------------------------------------------------------------------

def test_scenario_1_balance(talk):
    q = "რამდენი ANNUAL დღე დამრჩა?"
    (reply,), agent = talk(FakeLLM({q: X("BALANCE_QUERY", leave_type="ANNUAL")}), q)
    call = agent.tools.calls[0]
    assert reply.tools == ["get_leave_balance"]                           # MCP get_leave_balance
    assert call.arguments == {"leave_type": "ANNUAL"}                     # correct employee = the session's own
    assert "(2026 წელი)" in reply.text                                    # 2026
    for fragment in ("კუთვნილი: 25 + გადმოტანილი: 3",                      # correct calculation 25 + 3 − 15 − 3
                     "დამტკიცებული: 15", "განხილვის პროცესში: 3",           # approved / pending
                     "ხელმისაწვდომი: 10 სამუშაო დღე"):                      # available
        assert fragment in reply.text


# --- Scenario 2 — Annual request --------------------------------------------------------------------

def test_scenario_2_annual_request(talk, seeded_engine):
    m1 = "26 ოქტომბრიდან 30 ოქტომბრამდე შვებულება მინდა."
    m2 = "მაშინ 27-დან 30 ოქტომბრამდე."
    llm = FakeLLM({
        m1: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", start_date="2026-10-26", end_date="2026-10-30"),
        m2: X("PROVIDE_DETAILS", start_date="2026-10-27", end_date="2026-10-30"),
    })
    counts = []

    def count():
        counts.append(len(requests_in_db(seeded_engine)))

    (r1, r2, r3), agent = talk(llm, m1, count, m2, count, "დიახ", count)

    # identifies ANNUAL, checks notice: 26 Oct has only 4 full working days of notice (policy 4.4)
    assert agent.tools.calls[0].arguments["leave_type"] == "ANNUAL"
    assert "მუხლი 4.4" in r1.text and "2026-10-27" in r1.text
    # 27–30 Oct passes balance/overlap/probation/AUD checks: proposal + confirmation question
    for fragment in ("ტიპი: ყოველწლიური ანაზღაურებადი შვებულება", "პერიოდი: 2026-10-27 – 2026-10-30",
                     "დღეების რაოდენობა: 4 სამუშაო დღე", "ბალანსი: 10 → 6", "შევქმნა მოთხოვნა? (დიახ/არა)"):
        assert fragment in r2.text
    assert counts[:2] == [SEEDED_REQUESTS, SEEDED_REQUESTS]                # nothing before confirmation
    # creates a pending assistant request only after confirmation
    assert r3.tools == ["create_leave_request"] and "#28" in r3.text and counts[2] == SEEDED_REQUESTS + 1
    created = requests_in_db(seeded_engine)[-1]
    assert (created.request_id, created.employee_id, created.leave_type, created.status, created.created_via,
            created.days) == (28, "E1001", "ANNUAL", "pending", "assistant", 4)


@pytest.mark.parametrize("employee, start, end, expected", [
    ("E1002", "2026-11-02", "2026-11-06", "მუხლი 5.1"),                        # balance: 4 days left, 5 asked
    ("E1001", "2026-11-10", "2026-11-11", "ემთხვევა თქვენს სხვა მოთხოვნას"),    # overlap with pending #5
    ("E1004", "2026-11-24", "2026-11-26", "მუხლი 4.3"),                        # probation until 30 Nov
    ("E1001", "2026-12-14", "2026-12-18", "მუხლი 4.6"),                        # AUD restricted period
], ids=["balance", "overlap", "probation", "aud_restriction"])
def test_scenario_2_every_rule_is_checked(talk, seeded_engine, employee, start, end, expected):
    m = "შვებულება მინდა ამ თარიღებში"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=True,
                        start_date=start, end_date=end)})
    (r,), agent = talk(llm, m, employee=employee)
    assert r.tools == ["propose_leave_request"] and expected in r.text
    assert "შევქმნა მოთხოვნა?" not in r.text and agent.state.proposal is None
    assert len(requests_in_db(seeded_engine)) == SEEDED_REQUESTS


# --- Scenario 3 — Unpaid ----------------------------------------------------------------------------

def test_scenario_3_unpaid(talk, seeded_engine):
    m1, m2, m3 = "უხელფასო შვებულება მინდა.", "ოჯახური მიზეზი", "3-დან 6 ნოემბრამდე"
    llm = FakeLLM({
        m1: X("CREATE_LEAVE_REQUEST", leave_type="UNPAID", leave_type_explicit=True),
        m2: X("PROVIDE_DETAILS", reason="ოჯახური მიზეზი"),
        m3: X("PROVIDE_DETAILS", start_date="2026-11-03", end_date="2026-11-06"),
    })
    (r1, r2, r3, r4), _ = talk(llm, m1, m2, m3, "დიახ")
    assert r1.text.startswith("მოკლედ გთხოვთ მიუთითოთ მიზეზი")             # asks for the reason
    assert "ჯანმრთელობის დეტალების გარეშე" in r1.text                       # no health information
    assert "რომელი თარიღებით" in r2.text
    assert "4 კალენდარული დღე" in r3.text and "შევქმნა მოთხოვნა?" in r3.text  # notice/limit/overlap ok -> confirm
    assert "#28" in r4.text
    created = requests_in_db(seeded_engine)[-1]
    assert (created.leave_type, created.comment, created.status, created.created_via) == \
        ("UNPAID", "ოჯახური მიზეზი", "pending", "assistant")               # reason stored in comment


@pytest.mark.parametrize("start, end, expected", [
    ("2026-10-26", "2026-10-27", "მუხლი 7.2"),     # 10 working day notice
    ("2026-11-03", "2026-12-07", "მუხლი 7.1"),     # 35 calendar days > 30-day annual limit
], ids=["notice_10_days", "limit_30_days"])
def test_scenario_3_unpaid_checks(talk, seeded_engine, start, end, expected):
    m = "უხელფასო შვებულება მინდა, ოჯახური მიზეზით"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="UNPAID", leave_type_explicit=True,
                        start_date=start, end_date=end, reason="ოჯახური მიზეზი")})
    (r,), _ = talk(llm, m)
    assert expected in r.text and "შევქმნა მოთხოვნა?" not in r.text
    assert len(requests_in_db(seeded_engine)) == SEEDED_REQUESTS


def test_scenario_3_health_details_are_not_collected(talk, seeded_engine):
    m = "უხელფასო მინდა 3-დან 6 ნოემბრამდე, ოპერაცია მაქვს"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="UNPAID", leave_type_explicit=True,
                        start_date="2026-11-03", end_date="2026-11-06", reason="ოპერაცია მაქვს")})
    (r,), agent = talk(llm, m)
    assert "ჯანმრთელობის დეტალებს ნუ მიუთითებთ" in r.text and agent.state.awaiting == "reason"
    assert proposals_in_db(seeded_engine) == 0


# --- Scenario 4 — Sick ------------------------------------------------------------------------------

def test_scenario_4_sick_over_paid_balance(talk, seeded_engine):
    m = "ავადმყოფობის გამო 19-დან 29 ოქტომბრამდე ვერ ვიმუშავებ"     # 9 working days, 8 paid days left
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="SICK", leave_type_explicit=True,
                        start_date="2026-10-19", end_date="2026-10-29")})
    (r,), _ = talk(llm, m)
    assert "create_leave_request" not in r.tools                                      # not created
    assert "აღემატება თქვენს დარჩენილ ანაზღაურებად ავადმყოფობის დღეებს (8)" in r.text   # insufficient paid balance
    assert "არ ნიშნავს, რომ ავადმყოფობის აღრიცხვა აღარ შეიძლება" in r.text             # may still be recorded
    assert "ადამიანური რესურსების სამსახური" in r.text and "მუხლი 6.4" in r.text      # redirect to HR
    assert len(requests_in_db(seeded_engine)) == SEEDED_REQUESTS and proposals_in_db(seeded_engine) == 0


# --- Scenarios 5–7 — Bereavement, Study, Parental ---------------------------------------------------

def test_scenario_5_bereavement(talk, seeded_engine):
    m = "ბებიაჩემი გარდაიცვალა და შვებულება მინდა."
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="BEREAVEMENT", leave_type_explicit=True)},
                  answer="ბებიის გარდაცვალებისას ეკუთვნის 1 სამუშაო დღე [1].")
    (r,), _ = talk(llm, m)
    assert r.tools == []                                                    # identified; nothing proposed
    assert "1 სამუშაო დღე" in r.text and r.sources                          # explains the policy, with source
    assert "ასისტენტი ვერ ქმნის" in r.text                                  # does not create
    assert "HR პორტალით ან ადამიანური რესურსების სამსახურის მეშვეობით" in r.text   # redirect
    assert len(requests_in_db(seeded_engine)) == SEEDED_REQUESTS and proposals_in_db(seeded_engine) == 0


def test_scenario_6_study(talk, seeded_engine):
    m = "გამოცდისთვის შვებულება მინდა."
    (r,), _ = talk(FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="STUDY", leave_type_explicit=True)}), m)
    assert r.tools == []
    assert "ვერ ადასტურებს, არის თუ არა გამოცდა თქვენს დამტკიცებულ" in r.text   # does not assume plan approval
    assert "HR პორტალით" in r.text and "ასისტენტი ვერ ქმნის" in r.text          # redirect, not created
    assert len(requests_in_db(seeded_engine)) == SEEDED_REQUESTS and proposals_in_db(seeded_engine) == 0


def test_scenario_7_parental(talk, seeded_engine):
    m = "მშობლის შვებულება მინდა."
    (r,), _ = talk(FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="PARENTAL", leave_type_explicit=True)}), m)
    assert r.tools == []
    assert "პირდაპირ ადამიანური რესურსების სამსახურს" in r.text and "8 კვირით" in r.text   # HR process
    assert len(requests_in_db(seeded_engine)) == SEEDED_REQUESTS and proposals_in_db(seeded_engine) == 0


class NoAnswerLLM(FakeLLM):
    """Classifies, but cannot word an answer (e.g. the generation service is down)."""

    async def generate_text(self, system, prompt, on_chunk=None):
        raise LLMError("down")


@pytest.mark.parametrize("leave_type, facts", [
    ("BEREAVEMENT", ["არაუმეტეს 3 სამუშაო დღის", "1 სამუშაო დღე", "30 კალენდარული დღის"]),
    ("STUDY", ["არაუმეტეს 5 სამუშაო დღის", "დამტკიცებულ წლიურ გეგმაში"]),
    ("PARENTAL", ["დედობის, ბავშვის მოვლის, მამობის და შვილად აყვანის", "8 კვირით"]),
])
def test_scenarios_5_to_7_explain_rules_even_without_llm_answer(talk, leave_type, facts):
    """The explanation required by scenarios 5–7 (e.g. study: 5 working days + approved plan) is guaranteed."""
    m = "ეს შვებულება მინდა"
    (r,), _ = talk(NoAnswerLLM({m: X("CREATE_LEAVE_REQUEST", leave_type=leave_type, leave_type_explicit=True)}), m)
    assert r.tools == []
    for fact in facts:
        assert fact in r.text
    assert "შვებულებისა და გაცდენის პოლიტიკა v4.0" in r.sources


# --- Scenario 8 — Unauthorized access ---------------------------------------------------------------

def test_scenario_8_unauthorized_access(talk):
    q = "მაჩვენე E1002-ის ბალანსი."
    (reply,), agent = talk(FakeLLM({q: X("BALANCE_QUERY", other_employee_id="E1002")}), q)
    assert agent.tools.calls[0].ok is False                                # REJECT decided by the MCP server
    assert "სხვა თანამშრომლის მონაცემების ნახვა შეუძლებელია" in reply.text
    assert "ხელმისაწვდომი" not in reply.text and "კუთვნილი" not in reply.text   # no data exposed


# --- Scenario 9 — Existing request mutation ---------------------------------------------------------

def test_scenario_9_existing_request_mutation(talk, seeded_engine):
    before = [(req.request_id, req.status) for req in requests_in_db(seeded_engine)]
    m = "ჩემი მოთხოვნა გააუქმე."
    (r,), _ = talk(FakeLLM({m: X("MODIFY_EXISTING_REQUEST")}), m)
    assert r.tools == []                                                   # cancellation tool not used
    assert "არ შეუძლია უკვე წარდგენილი მოთხოვნის გაუქმება" in r.text          # explains
    assert "HR პორტალით" in r.text                                         # redirect
    assert [(req.request_id, req.status) for req in requests_in_db(seeded_engine)] == before   # nothing changed


# --- Scenario 10 — Duplicate confirmation -----------------------------------------------------------

def test_scenario_10_duplicate_confirmation(talk, seeded_engine):
    m = "27-დან 30 ოქტომბრამდე ყოველწლიური შვებულება მინდა"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=True,
                        start_date="2026-10-27", end_date="2026-10-30")})
    (_, first, second), _ = talk(llm, m, "დიახ", "დიახ")
    assert "#28" in first.text and "მოთხოვნა შეიქმნა" in first.text           # first creates one request
    assert second.tools == ["create_leave_request"]                         # same proposal confirmed again
    assert "უკვე შექმნილია" in second.text and "#28" in second.text            # returns the existing request
    assert len(requests_in_db(seeded_engine)) == SEEDED_REQUESTS + 1          # no duplicate inserted
