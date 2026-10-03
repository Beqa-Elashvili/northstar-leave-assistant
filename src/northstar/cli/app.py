"""CLI entry point: Georgian chat loop → HRAgent → (RAG | MCP server over stdio) → Supabase.

The MCP server runs as a subprocess with the employee identity fixed at start-up; the CLI never
talks to the leave tables directly. Diagnostics (including the server's stderr) go to log files
under `logs/`, never to the screen.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import anyio
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

import northstar
from northstar.agent.agent import HELP, HRAgent
from northstar.agent.llm import GeminiProvider, LLMError
from northstar.agent.policy_qa import PolicyAnswerer
from northstar.agent.tools import McpLeaveTools, ToolFailure
from northstar.cli.ui import COMMANDS_HELP, ChatUI
from northstar.clock import get_app_today
from northstar.config import PROJECT_ROOT, ConfigurationError, Settings, get_settings, require_database_url
from northstar.database.engine import get_session_factory
from northstar.rag.embeddings import EmbeddingError, get_embedding_provider
from northstar.rag.retrieval import Retriever

logger = logging.getLogger("northstar.cli")

LOG_DIR = PROJECT_ROOT / "logs"
CLI_LOG = "northstar-cli.log"
MCP_LOG = "mcp-server.log"
TOOL_TIMEOUT_SECONDS = 60
EXIT_COMMANDS = {"/exit", "/quit", "exit", "quit", "გასვლა"}
GOODBYE = "ნახვამდის!"


class StartupError(Exception):
    """The session could not start. The message is Georgian and safe to show."""


# --- process setup --------------------------------------------------------------------------------

def configure_stdio() -> None:
    """Georgian text must survive Windows consoles and pipes (cp1252 by default)."""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def configure_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / CLI_LOG
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    logging.getLogger("northstar").setLevel(logging.INFO)
    logging.captureWarnings(True)  # library warnings go to the log, not the screen
    return path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="northstar-cli", description="Northstar Services HR Assistant (Georgian)")
    parser.add_argument("--employee-id", default=None,
                        help="employee to act as; default DEMO_EMPLOYEE_ID (local development identity)")
    parser.add_argument("--no-stream", action="store_true", help="print policy answers only when complete")
    return parser.parse_args(argv)


# --- dependencies ---------------------------------------------------------------------------------

def build_dependencies(settings: Settings) -> tuple[GeminiProvider, Retriever]:
    try:
        require_database_url(settings)
    except ConfigurationError:
        raise StartupError("DATABASE_URL არ არის მითითებული. დააკოპირეთ .env.example → .env და შეავსეთ.") from None
    try:
        llm = GeminiProvider.from_settings(settings)
        embeddings = get_embedding_provider(settings)
    except (LLMError, EmbeddingError):
        raise StartupError("GEMINI_API_KEY არ არის მითითებული .env ფაილში.") from None
    return llm, Retriever(get_session_factory(), embeddings)


def server_parameters(employee_id: str) -> StdioServerParameters:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    src = str(Path(northstar.__file__).resolve().parents[1])  # works without `pip install` too
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH")) if p)
    return StdioServerParameters(command=sys.executable,
                                 args=["-m", "northstar.mcp.server", "--employee-id", employee_id],
                                 env=env, cwd=str(PROJECT_ROOT))


def server_failure_message(log_path: Path, offset: int) -> str:
    """Explain why the MCP server did not start, from the line it wrote to its own stderr log."""
    reason = None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            for line in fh:
                if line.startswith("MCP server:"):
                    reason = line.removeprefix("MCP server:").strip()
    except OSError:
        pass
    if reason and reason.startswith("database unavailable"):
        return "მონაცემთა ბაზასთან კავშირი ვერ მოხერხდა. შეამოწმეთ DATABASE_URL და ინტერნეტ-კავშირი."
    if reason:
        return f"HR სისტემამ სესია ვერ დაიწყო: {reason}"
    return "HR სისტემის (MCP სერვერის) გაშვება ვერ მოხერხდა."


def find_exception(exc: BaseException, kind: type[BaseException]) -> BaseException | None:
    """Look through (nested) exception groups raised by task groups."""
    if isinstance(exc, kind):
        return exc
    for inner in getattr(exc, "exceptions", ()):
        if found := find_exception(inner, kind):
            return found
    return None


# --- conversation loop ----------------------------------------------------------------------------

class ChatSession:
    def __init__(self, agent: HRAgent, ui: ChatUI, stream: bool = True):
        self.agent, self.ui, self.stream = agent, ui, stream

    async def run(self) -> None:
        while True:
            try:
                message = self.ui.ask().strip()
            except (EOFError, KeyboardInterrupt):
                self.ui.info(f"\n{GOODBYE}")
                return
            if not message:
                continue
            command = message.lower()
            if command in EXIT_COMMANDS:
                self.ui.info(GOODBYE)
                return
            if command == "/help":
                self.ui.info(f"{HELP}\n{COMMANDS_HELP}")
                continue
            if command == "/new":
                self.agent.reset()
                self.ui.info("დაიწყო ახალი საუბარი.")
                continue
            if command.startswith("/"):
                self.ui.warning(f"უცნობი ბრძანება. {COMMANDS_HELP}")
                continue
            await self.turn(message)

    async def turn(self, message: str) -> None:
        self.ui.begin_turn()
        try:
            reply = await self.agent.handle(message, on_chunk=self.ui.on_chunk if self.stream else None)
        finally:
            self.ui.stop_status()
        self.ui.end_turn(reply)


async def run_cli(settings: Settings, employee_id: str, ui: ChatUI, stream: bool, log_dir: Path) -> None:
    llm, retriever = build_dependencies(settings)
    try:
        rag_ready = await anyio.to_thread.run_sync(retriever.is_ready)
    except SQLAlchemyError:
        logger.exception("database unavailable at start-up")
        raise StartupError("მონაცემთა ბაზასთან კავშირი ვერ მოხერხდა. შეამოწმეთ DATABASE_URL და ინტერნეტ-კავშირი.") \
            from None

    log_path = log_dir / MCP_LOG
    started = False
    with open(log_path, "a", encoding="utf-8") as errlog:
        offset = errlog.tell()
        try:
            async with Client(stdio_client(server_parameters(employee_id), errlog=errlog),
                              read_timeout_seconds=TOOL_TIMEOUT_SECONDS) as client:
                agent = HRAgent(McpLeaveTools(client), llm, PolicyAnswerer(retriever, llm), get_app_today())
                try:
                    profile = await agent.start()
                except ToolFailure as exc:
                    raise StartupError(exc.message) from None
                started = True
                ui.header(profile["employee_id"], profile["full_name"], get_app_today().isoformat())
                if not rag_ready:
                    ui.warning("პოლიტიკის დოკუმენტები ჯერ არ არის ჩატვირთული — პოლიტიკის კითხვებზე პასუხი "
                               "შეზღუდული იქნება. გაუშვით: python -m scripts.ingest_documents")
                await ChatSession(agent, ui, stream).run()
        except Exception as exc:
            if started:
                raise
            if found := find_exception(exc, StartupError):
                raise found from None
            logger.exception("MCP session failed to start")
            errlog.flush()
            raise StartupError(server_failure_message(log_path, offset)) from None


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = parse_args(argv)
    ui = ChatUI()
    log_path = configure_logging(LOG_DIR)
    try:
        settings = get_settings()
    except ValidationError as exc:
        fields = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
        ui.error(f"კონფიგურაციის შეცდომა .env ფაილში: {', '.join(fields)}")
        return 2
    employee_id = (args.employee_id or settings.demo_employee_id or "").strip().upper()
    if not employee_id:
        ui.error("თანამშრომელი არ არის მითითებული: გამოიყენეთ --employee-id ან DEMO_EMPLOYEE_ID .env ფაილში.")
        return 2
    try:
        anyio.run(run_cli, settings, employee_id, ui, not args.no_stream, LOG_DIR)
    except BaseException as exc:  # noqa: BLE001 - the CLI must never show a stack trace
        ui.stop_status()
        if find_exception(exc, KeyboardInterrupt):
            ui.info(f"\n{GOODBYE}")
            return 0
        if startup := find_exception(exc, StartupError):
            ui.error(str(startup))
            ui.info(f"დეტალები: {log_path}")
            return 2
        if isinstance(exc, SystemExit):
            raise
        logger.exception("unexpected CLI failure")
        ui.error("მოულოდნელი შეცდომა მოხდა და სესია დასრულდა.")
        ui.info(f"დეტალები: {log_path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
