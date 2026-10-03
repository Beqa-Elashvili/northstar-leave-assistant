"""Georgian CLI: rendering, commands, streaming, safe start-up failures (LLM mocked or never called)."""

import io
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp.client.client import Client
from rich.console import Console
from sqlalchemy.orm import sessionmaker

from northstar.agent.agent import AgentReply, HRAgent
from northstar.agent.policy_qa import PolicyAnswerer
from northstar.agent.tools import McpLeaveTools
from northstar.cli.app import ChatSession, StartupError, find_exception, server_failure_message
from northstar.cli.ui import ChatUI
from northstar.mcp.server import build_server
from northstar.services.authorization import Principal, Role
from tests.agent_helpers import X, FakeLLM, ingest_test_documents
from tests.mcp_helpers import CLOCK

ROOT = Path(__file__).resolve().parents[1]


def make_ui():
    out = io.StringIO()
    return ChatUI(Console(file=out, width=120, highlight=False, color_system=None)), out


# --- rendering ------------------------------------------------------------------------------------

def test_header_shows_employee():
    ui, out = make_ui()
    ui.header("E1001", "ნინო ბერიძე", "2026-10-19")
    text = out.getvalue()
    assert "Northstar Services HR Assistant" in text
    assert "თანამშრომელი: E1001" in text and "სახელი: ნინო ბერიძე" in text and "/exit" in text


def test_tool_reply_shows_tool_assistant_and_source_labels():
    ui, out = make_ui()
    ui.begin_turn()
    ui.end_turn(AgentReply("ბალანსი: 10", sources=["პოლიტიკა v4.0, მუხლი 5.1"], tools=["get_leave_balance"]))
    text = out.getvalue()
    assert "ხელსაწყო: get_leave_balance (ბალანსის მიღება)" in text
    assert text.index("ხელსაწყო:") < text.index("ასისტენტი:") < text.index("ბალანსი: 10") < text.index("წყარო:")


def test_streamed_answer_is_not_printed_twice():
    ui, out = make_ui()
    ui.begin_turn()
    ui.on_chunk("პასუხი ")
    ui.on_chunk("[1].")
    ui.end_turn(AgentReply("პასუხი [1].", sources=["[1] დოკუმენტი"], streamed=True))
    text = out.getvalue()
    assert text.count("პასუხი [1].") == 1 and text.count("ასისტენტი:") == 1
    assert "წყარო: [1] დოკუმენტი" in text


def test_broken_stream_still_shows_final_message():
    ui, out = make_ui()
    ui.begin_turn()
    ui.on_chunk("ნაწილობრივი")
    ui.end_turn(AgentReply("ბოდიში, AI სერვისი ამ წუთას მიუწვდომელია."))
    assert "ნაწილობრივი" in out.getvalue() and "მიუწვდომელია" in out.getvalue()


def test_markup_in_text_is_not_interpreted():
    ui, out = make_ui()
    ui.begin_turn()
    ui.end_turn(AgentReply("[bold]არა markup[/bold]"))
    assert "[bold]არა markup[/bold]" in out.getvalue()


# --- start-up failure helpers ---------------------------------------------------------------------

def test_server_failure_message_uses_server_reason(tmp_path):
    log = tmp_path / "mcp.log"
    log.write_text("old run\nMCP server: ძველი\n", encoding="utf-8")
    offset = log.stat().st_size
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("MCP server: თანამშრომელი E9999 ვერ მოიძებნა.\n")
    assert server_failure_message(log, offset) == "HR სისტემამ სესია ვერ დაიწყო: თანამშრომელი E9999 ვერ მოიძებნა."


def test_server_failure_message_for_database_and_unknown(tmp_path):
    log = tmp_path / "mcp.log"
    log.write_text("MCP server: database unavailable (OperationalError)\n", encoding="utf-8")
    assert "მონაცემთა ბაზასთან კავშირი ვერ მოხერხდა" in server_failure_message(log, 0)
    assert server_failure_message(tmp_path / "missing.log", 0) == "HR სისტემის (MCP სერვერის) გაშვება ვერ მოხერხდა."


def test_find_exception_in_nested_groups():
    inner = StartupError("x")
    group = BaseExceptionGroup("outer", [ValueError(), ExceptionGroup("inner", [inner])])
    assert find_exception(group, StartupError) is inner
    assert find_exception(ValueError(), StartupError) is None


# --- conversation loop (real MCP server in memory, scripted LLM) ----------------------------------

pytest_db = pytest.mark.db


@pytest.fixture(scope="module")
def retriever(engine):
    return ingest_test_documents(engine)


def run_session(engine, retriever, llm, inputs, stream=True):
    ui, out = make_ui()
    feed = iter(inputs)

    def ask():
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    ui.ask = ask

    async def go():
        server = build_server(Principal(Role.EMPLOYEE, "E1001"), sessionmaker(bind=engine, expire_on_commit=False),
                              CLOCK)
        async with Client(server) as client:
            agent = HRAgent(McpLeaveTools(client), llm, PolicyAnswerer(retriever, llm), CLOCK.today())
            await agent.start()
            first_conversation = agent.state.conversation_id
            await ChatSession(agent, ui, stream).run()
            return agent, first_conversation

    agent, first = anyio.run(go)
    return out.getvalue(), agent, first


@pytest_db
def test_session_commands_and_turns(seeded_engine, retriever):
    q = "რამდენი დღე დამრჩა?"
    llm = FakeLLM({q: X("BALANCE_QUERY")})
    text, agent, first = run_session(seeded_engine, retriever, llm, ["", "/help", q, "/foo", "/new", "/exit", q])
    assert "ბრძანებები:" in text and "უცნობი ბრძანება" in text and "დაიწყო ახალი საუბარი" in text
    assert "ხელსაწყო: get_leave_balance" in text and "ხელმისაწვდომი: 10 სამუშაო დღე" in text
    assert agent.state.conversation_id != first and agent.state.employee_id == "E1001"
    assert llm.structured_calls == 1          # "/exit" stopped the loop before the last message
    assert text.rstrip().endswith("ნახვამდის!")


@pytest_db
def test_new_conversation_forgets_the_draft(seeded_engine, retriever):
    m1, m2 = "27-დან 30 ოქტომბრამდე შვებულება მინდა", "დიახ"
    llm = FakeLLM({m1: X("CREATE_LEAVE_REQUEST", leave_type="ANNUAL", start_date="2026-10-27",
                         end_date="2026-10-30"),
                   m2: X("CONFIRM")})
    text, agent, _ = run_session(seeded_engine, retriever, llm, [m1, "/new", m2])
    assert "შევქმნა მოთხოვნა?" in text
    assert "დასადასტურებელი მოთხოვნა ამ წუთას არ არის" in text   # confirmation of an abandoned summary is refused
    assert "create_leave_request" not in text


@pytest_db
def test_policy_answer_streams_with_sources(seeded_engine, retriever):
    q = "რამდენი დღით ადრე უნდა მოვითხოვო ყოველწლიური შვებულება?"
    llm = FakeLLM({q: X("POLICY_QUESTION")}, answer="5 სამუშაო დღით ადრე [1].")
    text, _, _ = run_session(seeded_engine, retriever, llm, [q])
    assert text.count("5 სამუშაო დღით ადრე [1].") == 1
    assert "წყარო: [1]" in text and "ხელსაწყო:" not in text


# --- the real entry point as a subprocess ---------------------------------------------------------

def cli(db_url, db_schema, *args, stdin="/exit\n", **env_overrides):
    env = dict(os.environ)
    env.update({"DATABASE_URL": db_url, "DB_SCHEMA": db_schema, "GEMINI_API_KEY": "test-key-not-used",
                "EMBEDDING_PROVIDER": "local", "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"})
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-m", "northstar.cli", *args], input=stdin, env=env, cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


@pytest_db
def test_cli_starts_and_exits_cleanly(seeded_engine, db_url, db_schema):
    result = cli(db_url, db_schema)
    assert result.returncode == 0
    assert "Northstar Services HR Assistant" in result.stdout and "ნინო ბერიძე" in result.stdout
    assert "ნახვამდის!" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert "test-key-not-used" not in result.stdout and db_url not in result.stdout


@pytest_db
def test_cli_unknown_employee_fails_safely(seeded_engine, db_url, db_schema):
    result = cli(db_url, db_schema, "--employee-id", "E9999")
    assert result.returncode == 2
    # generic on purpose: the server does not reveal which employee IDs exist
    assert "HR სისტემამ სესია ვერ დაიწყო: მომხმარებლის იდენტიფიკაცია ვერ მოხერხდა." in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


def test_cli_missing_key_fails_safely(db_url):
    result = cli(db_url, "public", GEMINI_API_KEY="", EMBEDDING_PROVIDER="gemini")
    assert result.returncode == 2
    assert "GEMINI_API_KEY არ არის მითითებული" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


def test_cli_unreachable_database_fails_safely():
    bad = "postgresql://user:secret-password@127.0.0.1:1/nodb"
    result = cli(bad, "public")
    assert result.returncode == 2
    assert "მონაცემთა ბაზასთან კავშირი ვერ მოხერხდა" in result.stdout
    assert "secret-password" not in result.stdout + result.stderr and "Traceback" not in result.stdout + result.stderr
