"""Terminal rendering: clearly separated You / Assistant / Source / Tool blocks in Georgian.

Only user-facing text reaches the screen. Prompts, stack traces, tool arguments, connection
strings and keys never do — diagnostics go to the log file.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from northstar.agent.agent import AgentReply

LABEL_YOU = "თქვენ:"
LABEL_ASSISTANT = "ასისტენტი:"
LABEL_SOURCE = "წყარო:"
LABEL_TOOL = "ხელსაწყო:"

TOOL_DESCRIPTIONS = {
    "get_my_profile": "პროფილის მიღება",
    "list_leave_types": "შვებულების ტიპების სია",
    "get_leave_balance": "ბალანსის მიღება",
    "list_leave_requests": "მოთხოვნების სია",
    "propose_leave_request": "მოთხოვნის წესებით შემოწმება",
    "create_leave_request": "მოთხოვნის შექმნა",
    "decline_leave_proposal": "შეთავაზების გაუქმება",
}

COMMANDS_HELP = "ბრძანებები: /help — დახმარება · /new — ახალი საუბარი · /exit — გასვლა"


class ChatUI:
    def __init__(self, console: Console | None = None):
        self.console = console or Console(highlight=False, soft_wrap=True)
        self._streamed: list[str] = []
        self._status = None

    # --- session frame ----------------------------------------------------------------------------

    def header(self, employee_id: str, full_name: str, today: str) -> None:
        body = Text()
        body.append("თანამშრომელი: ", style="dim").append(f"{employee_id}\n")
        body.append("სახელი: ", style="dim").append(f"{full_name}\n")
        body.append("თარიღი: ", style="dim").append(today)
        self.console.print(Panel(body, title="[bold]Northstar Services HR Assistant[/]", title_align="left",
                                 border_style="cyan", expand=False))
        self.console.print(COMMANDS_HELP, style="dim")

    def ask(self) -> str:
        self.console.print()
        return self.console.input(f"[bold green]{LABEL_YOU}[/] ")

    def info(self, text: str) -> None:
        self.console.print(text, style="dim")

    def warning(self, text: str) -> None:
        self.console.print(Text(text, style="yellow"))

    def error(self, text: str) -> None:
        self.console.print(Text(text, style="bold red"))

    # --- one turn ---------------------------------------------------------------------------------

    def begin_turn(self) -> None:
        self._streamed = []
        self._status = self.console.status("[dim]ვამუშავებ…[/]", spinner="dots")
        self._status.start()

    def stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def on_chunk(self, chunk: str) -> None:
        """Streaming callback: the first chunk replaces the spinner with the Assistant label."""
        if not self._streamed:
            self.stop_status()
            self.console.print()
            self.console.print(LABEL_ASSISTANT, style="bold cyan")
        self._streamed.append(chunk)
        self.console.print(chunk, end="", markup=False, highlight=False)

    def end_turn(self, reply: AgentReply) -> None:
        self.stop_status()
        streamed = "".join(self._streamed).strip()
        if streamed:
            self.console.print()
            if streamed != reply.text.strip():  # e.g. the stream broke off: show the final message too
                self.console.print(Text(reply.text))
        else:
            self.console.print()
            for name in reply.tools:
                self._tool_line(name)
            self.console.print(LABEL_ASSISTANT, style="bold cyan")
            self.console.print(Text(reply.text))
        for source in reply.sources:
            line = Text()
            line.append(f"{LABEL_SOURCE} ", style="bold magenta").append(source, style="magenta")
            self.console.print(line)
        if streamed:
            for name in reply.tools:
                self._tool_line(name)

    def _tool_line(self, name: str) -> None:
        line = Text()
        line.append(f"{LABEL_TOOL} ", style="bold blue").append(name, style="blue")
        if name in TOOL_DESCRIPTIONS:
            line.append(f" ({TOOL_DESCRIPTIONS[name]})", style="dim")
        self.console.print(line)
