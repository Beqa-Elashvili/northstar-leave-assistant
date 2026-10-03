"""Assignment section 50: every listed error is handled gracefully, with a Georgian message.

Business-rule errors (insufficient balance, overlap, notice, restricted period, probation) are
covered end to end in test_scenarios.py; here the remaining items are checked through the agent
and the MCP server: no stack traces, no internal details, no partial writes.
"""

import anyio
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from northstar.agent.agent import HRAgent
from northstar.agent.policy_qa import PolicyAnswerer
from northstar.agent.tools import McpLeaveTools
from northstar.database.engine import make_engine
from northstar.rag.embeddings import EmbeddingError
from northstar.rag.retrieval import RagNotReady
from northstar.services.authorization import Role
from tests.agent_helpers import X, BrokenLLM, FakeLLM, converse, ingest_test_documents
from tests.mcp_helpers import CLOCK, McpHarness, ToolCallError

pytestmark = pytest.mark.db

TECHNICAL = ("Traceback", "Error:", "Exception", "psycopg2", "sqlalchemy", "SELECT", "postgresql://")


def assert_user_safe(text: str) -> None:
    for marker in TECHNICAL:
        assert marker not in text


@pytest.fixture(scope="module")
def retriever(engine):
    return ingest_test_documents(engine)


@pytest.fixture()
def talk(seeded_engine, retriever):
    def run(llm, *messages, employee="E1001", rag=None):
        return converse(seeded_engine, rag or retriever, llm, list(messages), employee)
    return run


# --- invalid dates ----------------------------------------------------------------------------------

@pytest.mark.parametrize("start, end, expected", [
    ("2026-11-31", None, "თარიღი ვერ გავიგე"),                                  # not a calendar date
    ("2026-11-13", "2026-11-10", "დასრულების თარიღი დაწყების თარიღზე ადრეა"),     # reversed range
    ("2026-12-28", "2027-01-05", "მუხლი 2.1"),                                   # crosses the leave year
])
def test_invalid_dates(talk, start, end, expected):
    m = "შვებულება მინდა"
    llm = FakeLLM({m: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", leave_type_explicit=True,
                        start_date=start, end_date=end)})
    (r,), agent = talk(llm, m)
    assert expected in r.text and agent.state.proposal is None
    assert_user_safe(r.text)


def test_invalid_date_arguments_rejected_by_mcp_schema(seeded_engine):
    with pytest.raises(ToolCallError):
        McpHarness(seeded_engine).call("propose_leave_request", conversation_id="00000000-0000-0000-0000-000000000000",
                                       leave_type="ANNUAL", start_date="2026-13-01", end_date="2026-13-02")


# --- invalid employee / unknown leave type -----------------------------------------------------------

def test_invalid_employee_identity(seeded_engine):
    err = pytest.raises(ToolCallError, McpHarness(seeded_engine, "E9999").call, "get_leave_balance").value
    assert err.code == "authentication_error"
    assert_user_safe(err.payload["message"])


def test_invalid_employee_id_argument(seeded_engine):
    with pytest.raises(ToolCallError):   # pattern E\d{4} is part of the typed schema
        McpHarness(seeded_engine).call("get_leave_balance", employee_id="'; DROP TABLE employees; --")


def test_unknown_leave_type(seeded_engine):
    with pytest.raises(ToolCallError):   # leave_type is an enum of the six supplied codes
        McpHarness(seeded_engine).call("propose_leave_request", conversation_id="00000000-0000-0000-0000-000000000000",
                                       leave_type="VACATION", start_date="2026-11-02", end_date="2026-11-03")


# --- unsupported assistant operation ---------------------------------------------------------------

@pytest.mark.parametrize("tool, args", [
    ("approve_leave_request", {"request_id": 5}),
    ("reject_leave_request", {"request_id": 5, "reason": "x"}),
    ("cancel_leave_request", {"request_id": 5}),
])
def test_unsupported_operation_is_refused_by_the_agent_and_the_server(seeded_engine, tool, args):
    agent_tools = McpLeaveTools(client=None)

    async def via_agent():
        await agent_tools._call(tool, **args)

    with pytest.raises(Exception) as exc:
        anyio.run(via_agent)
    assert exc.value.code == "tool_not_allowed"                     # the agent cannot even send it
    err = pytest.raises(ToolCallError, McpHarness(seeded_engine).call, tool, **args).value
    assert err.code == "permission_denied" and "4.8" in err.payload["article"]


# --- MCP failure -----------------------------------------------------------------------------------

class DeadMcpClient:
    async def call_tool(self, name, arguments):
        raise ConnectionError("broken pipe")


def test_mcp_failure(retriever):
    q = "რამდენი დღე დამრჩა?"
    agent = HRAgent(McpLeaveTools(DeadMcpClient()), FakeLLM({q: X("BALANCE_QUERY")}),
                    PolicyAnswerer(retriever, FakeLLM()), CLOCK.today())
    r = anyio.run(agent.handle, q)
    assert r.text == "HR სისტემასთან (MCP სერვერთან) კავშირი ვერ მოხერხდა. სცადეთ მოგვიანებით."
    assert_user_safe(r.text)


# --- database failure ------------------------------------------------------------------------------

def test_database_failure_in_mcp_server():
    dead = sessionmaker(bind=make_engine("postgresql://u:secret-pw@127.0.0.1:1/none"))
    from northstar.mcp.server import build_server
    from northstar.services.authorization import Principal

    harness = McpHarness.__new__(McpHarness)
    harness.server = build_server(Principal(Role.EMPLOYEE, "E1001"), dead, CLOCK)
    err = pytest.raises(ToolCallError, harness.call, "get_leave_balance").value
    assert err.code == "database_error"
    assert err.payload["message"] == "მონაცემთა ბაზასთან კავშირი ვერ მოხერხდა. სცადეთ მოგვიანებით."
    assert "secret-pw" not in str(err.payload)


# --- RAG failure -----------------------------------------------------------------------------------

class FailingRetriever:
    def __init__(self, exc):
        self.exc = exc

    def retrieve(self, *args, **kwargs):
        raise self.exc


@pytest.mark.parametrize("exc, expected", [
    (RagNotReady("not ingested"), "პოლიტიკის დოკუმენტების საძიებო ბაზა მზად არ არის"),
    (EmbeddingError("HTTP 503"), "პოლიტიკის დოკუმენტებში ძიება ამ წუთას მიუწვდომელია"),
    (OperationalError("SELECT 1", {}, Exception("server closed")), "დოკუმენტების ბაზასთან კავშირი ვერ მოხერხდა"),
], ids=["not_ingested", "embedding_service", "document_store"])
def test_rag_failure(talk, exc, expected):
    q = "რამდენი დღით ადრე უნდა მოვითხოვო შვებულება?"
    (r,), _ = talk(FakeLLM({q: X("POLICY_QUESTION")}), q, rag=FailingRetriever(exc))
    assert expected in r.text and r.sources == []
    assert_user_safe(r.text)


# --- LLM failure -----------------------------------------------------------------------------------

def test_llm_failure(talk, seeded_engine):
    (r,), _ = talk(BrokenLLM(), "27-დან 30 ოქტომბრამდე შვებულება მინდა")
    assert "AI სერვისი ამ წუთას მიუწვდომელია" in r.text and r.tools == []
    assert_user_safe(r.text)
