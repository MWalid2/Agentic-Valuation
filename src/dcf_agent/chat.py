"""Terminal chat over the valuation graph (Typer + Rich).

The first message names a company and a date ("Apple, 2026-10-11", optionally with
a question); Gate 0 confirms them; each step's progress is shown; answers are
tables with numbered sources. Later messages are questions about the same company
and reuse the data already collected. Sessions live in the SQLite checkpoint, so a
closed chat can be resumed.

Commands: /sources, /state, /resume [id], /new, /help, /quit.
"""

import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer
from langgraph.types import Command
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from dcf_agent.graph.workflow import default_budget, pending_gate, thread

HELP = (
    "First message: a company and a date, e.g. [bold]Apple, 2026-10-11[/] "
    "(optionally followed by a question). Then ask questions about it.\n"
    "/sources  list the sources of the last answer\n"
    "/state    show the session (company, date, steps, decisions, budget)\n"
    "/resume   list saved sessions; /resume ID switches to one\n"
    "/new      start over with another company\n"
    "/quit     leave (the session is saved)"
)
STEPS = {
    "router": "Understanding the request",
    "followup": "Reading the question",
    "gate0": "Confirmation",
    "data": "Collecting SEC filings, prices, FRED and Damodaran data",
    "data_review": "Data review",
    "wacc": "Computing the cost of capital",
    "unwired": "Skipping a step not available yet",
    "answer": "Writing the answer",
    "halt": "Budget check",
}
Ask = Callable[[str], str]


def sessions(conn: sqlite3.Connection) -> list[str]:
    """Saved session IDs, most recent first."""
    rows = conn.execute(
        "SELECT thread_id, MAX(rowid) AS last FROM checkpoints "
        "GROUP BY thread_id ORDER BY last DESC"
    ).fetchall()
    return [row[0] for row in rows]


class Chat:
    def __init__(
        self,
        graph: Any,
        console: Console,
        ask: Ask,
        conn: sqlite3.Connection,
        session: str | None = None,
    ) -> None:
        self.graph, self.console, self.ask, self.conn = graph, console, ask, conn
        self.session = session

    # Session state -------------------------------------------------------------------

    def values(self) -> dict[str, Any]:
        if self.session is None:
            return {}
        return dict(self.graph.get_state(thread(self.session)).values)

    def active(self) -> bool:
        """A session that has confirmed a company and can take questions."""
        values = self.values()
        return values.get("request") is not None and values.get("status") == "done"

    # Main loop --------------------------------------------------------------------------

    def run(self) -> None:
        self.console.print(Panel(HELP, title="DCF agent", border_style="cyan"))
        if self.session is not None:
            self.show_session()
        while True:
            try:
                text = self.ask("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                text = "/quit"
            if not text:
                continue
            if text.startswith("/"):
                if not self.command(text):
                    break
                continue
            self.send(text)

    def command(self, text: str) -> bool:
        name, _, argument = text.partition(" ")
        if name == "/quit":
            if self.session:
                self.console.print(f"Session saved: [bold]{self.session}[/] (/resume to return)")
            return False
        if name == "/help":
            self.console.print(HELP)
        elif name == "/sources":
            self.show_sources()
        elif name == "/state":
            self.show_state()
        elif name == "/new":
            self.session = None
            self.console.print("New session: name a company and a date.")
        elif name == "/resume":
            self.resume(argument.strip())
        else:
            self.console.print(f"Unknown command {name}; /help lists them.")
        return True

    def send(self, text: str) -> None:
        if not self.active():
            # A new session (or one that stopped): the message goes to the Router.
            self.session = uuid.uuid4().hex[:12]
            payload: Any = {"message": text, "budget": default_budget()}
            self.console.print(f"[dim]Session {self.session}[/]")
        else:
            payload = {"message": text}
        self.advance(payload)

    def advance(self, payload: Any) -> None:
        """Run the graph until it finishes, asking at every gate on the way."""
        while True:
            self.stream(payload)
            gate = pending_gate(self.graph, self.session) if self.session else None
            if gate is None:
                break
            decision = self.gate0(gate) if gate["gate"] == 0 else self.gate1(gate)
            payload = Command(resume=decision)
        self.show_answer()

    def stream(self, payload: Any) -> None:
        assert self.session is not None
        config = thread(self.session)
        started = time.monotonic()
        with self.console.status("Working...") as status:
            for update in self.graph.stream(payload, config, stream_mode="updates"):
                for node in update:
                    if node.startswith("__") or node not in STEPS:
                        continue
                    elapsed = time.monotonic() - started
                    if node not in ("gate0", "data_review"):
                        self.console.print(f"  [green]done[/] {STEPS[node]} ({elapsed:.1f}s)")
                    started = time.monotonic()
                upcoming = self.graph.get_state(config).next
                if upcoming and upcoming[0] in STEPS:
                    status.update(STEPS[upcoming[0]] + "...")

    # Gates ------------------------------------------------------------------------------

    def gate0(self, gate: dict[str, Any]) -> dict[str, Any]:
        self.show_gate0(gate)
        if gate["status"] == "ambiguous" or gate["status"] == "not_found":
            tickers = [c["ticker"] for c in gate["candidates"]]
            answer = self.ask(
                "Which company? Type its ticker"
                + (f" ({', '.join(tickers)})" if tickers else "")
                + ", or 'stop': "
            ).strip()
            if answer.lower() in ("stop", ""):
                return {"action": "stop"}
            return {"action": "choose", "ticker": answer.upper()}
        if gate["date_error"]:
            answer = self.ask("Valuation date (YYYY-MM-DD), or 'stop': ").strip()
            if answer.lower() in ("stop", ""):
                return {"action": "stop"}
            return {"action": "redate", "valuation_date": answer}
        if gate["scope"] and gate["scope"]["status"] == "blocked":
            answer = self.ask("Out of scope. Type another ticker, or press Enter to stop: ").strip()
            return {"action": "choose", "ticker": answer.upper()} if answer else {"action": "stop"}
        answer = self.ask(
            "Enter to confirm; or 'stop', 'date YYYY-MM-DD', 'ticker XYZ', 'industry <label>': "
        ).strip()
        word, _, rest = answer.partition(" ")
        word = word.lower()
        if word == "stop":
            return {"action": "stop"}
        if word == "date" and rest:
            return {"action": "redate", "valuation_date": rest.strip()}
        if word == "ticker" and rest:
            return {"action": "choose", "ticker": rest.strip().upper()}
        decision: dict[str, Any] = {"action": "confirm"}
        if word == "industry" and rest:
            decision["industry"] = rest.strip()
        if gate["intent"] is None:
            choice = self.ask(
                "1) ask a question  2) quick value  3) full Excel model [1]: "
            ).strip()
            if choice == "2":
                decision["output"] = "quick"
            elif choice == "3":
                decision["output"] = "excel"
            else:
                decision["output"] = "question"
                decision["question"] = self.ask("Your question: ").strip() or "Show the latest data"
        return decision

    def gate1(self, gate: dict[str, Any]) -> dict[str, Any]:
        self.console.print(
            Panel(gate["question"] or "", title="Data review", border_style="yellow")
        )
        answer = self.ask("continue or stop? [continue]: ").strip().lower()
        return {"action": "stop" if answer == "stop" else "continue"}

    # Display ----------------------------------------------------------------------------

    def show_gate0(self, gate: dict[str, Any]) -> None:
        table = Table(show_header=False, box=None)
        company = gate["company"]
        if company:
            table.add_row(
                "Company", f"{company['name']} ({company['ticker']}), CIK {company['cik']}"
            )
        else:
            table.add_row("Company", f"[yellow]{gate['status'].replace('_', ' ')}[/]")
        when = gate["valuation_date"] or f"[red]{gate['date_error']}[/]"
        source = gate["date_source"]
        shown = f" [dim]({source})[/]" if source and source != gate["valuation_date"] else ""
        table.add_row("Valuation date", f"{when}{shown}")
        if gate["scope"]:
            scope = gate["scope"]
            verdict = "[red]out of scope[/]" if scope["status"] == "blocked" else "in scope"
            table.add_row("Scope", verdict + "".join(f"\n  {r}" for r in scope["reasons"]))
        if gate["question"]:
            table.add_row("Question", f"{gate['question']} [dim]({gate['intent']})[/]")
        if gate["plan"]:
            table.add_row("Plan", " > ".join(gate["plan"]))
        for note in gate["notes"]:
            table.add_row("Note", note)
        self.console.print(Panel(table, title="Gate 0: confirm", border_style="cyan"))
        if gate["status"] != "resolved" and gate["candidates"]:
            candidates = Table("Ticker", "Company", "Match")
            for c in gate["candidates"]:
                candidates.add_row(c["ticker"], c["name"], c["match"])
            self.console.print(candidates)

    def show_answer(self) -> None:
        values = self.values()
        reply = values.get("reply")
        status = values.get("status")
        if reply is not None and status == "done" and values.get("answer"):
            self.console.print(Panel(Markdown(reply.body), title=reply.title, border_style="green"))
            if reply.sources:
                self.console.print(f"[dim]{len(reply.sources)} sources: /sources to list them[/]")
        elif values.get("answer"):
            self.console.print(values["answer"])
        if status in ("stopped", "failed", "budget_exhausted"):
            self.console.print(f"[yellow]Session {status}.[/] Name a company to start again.")

    def show_sources(self) -> None:
        reply = self.values().get("reply")
        if reply is None or not reply.sources:
            self.console.print("No sources yet.")
            return
        table = Table("#", "Source", "Filed", "Period end", "Reference", show_lines=True)
        for s in reply.sources:
            note = f"\n[dim]{s.note}[/]" if s.note else ""
            table.add_row(
                str(s.number),
                s.source,
                str(s.filed or ""),
                str(s.period_end or ""),
                s.ref + note,
            )
        self.console.print(table)

    def show_state(self) -> None:
        values = self.values()
        if not values:
            self.console.print("No session yet.")
            return
        table = Table(show_header=False, box=None)
        table.add_row("Session", str(self.session))
        request = values.get("request")
        if request is not None:
            table.add_row("Company", f"{request.company_name} ({request.ticker})")
            table.add_row("Valuation date", str(request.valuation_date))
            table.add_row("Last question", f"{request.question} [dim]({request.intent})[/]")
            table.add_row("Plan", " > ".join(request.plan))
        table.add_row("Steps done", ", ".join(values.get("done", ())) or "none")
        table.add_row("Status", str(values.get("status")))
        bundle = values.get("data")
        if bundle is not None:
            blocked = [k for k, r in bundle.readiness.items() if r.status == "blocked"]
            table.add_row("Readiness blocked", ", ".join(blocked) or "none")
            if bundle.industry is not None:
                table.add_row("Industry", f"{bundle.industry.name} ({bundle.industry.method})")
        review = values.get("data_review")
        if review is not None:
            table.add_row("Data review", f"{review.status}; {len(review.steps)} steps")
        decision = values.get("wacc_decision")
        if decision is not None:
            table.add_row(
                "Beta method",
                f"{decision.method} by {decision.source}"
                + (" [yellow](review)[/]" if decision.flagged else ""),
            )
        budget = values.get("budget")
        if budget is not None:
            table.add_row(
                "Budget (this run)",
                f"{budget.steps_used}/{budget.max_steps} steps, {budget.wall_seconds_used:.1f}s",
            )
        table.add_row("Approvals", str(len(values.get("approvals", ()))))
        table.add_row("Audit events", str(len(values.get("audit_log", ()))))
        self.console.print(Panel(table, title="Session state"))

    def show_session(self) -> None:
        if self.session is None:
            return
        gate = pending_gate(self.graph, self.session)
        if gate is not None:
            self.console.print(f"Session {self.session} is waiting at Gate {gate['gate']}.")
            decision = self.gate0(gate) if gate["gate"] == 0 else self.gate1(gate)
            self.advance(Command(resume=decision))
            return
        values = self.values()
        request = values.get("request")
        if request is not None:
            self.console.print(
                f"Session {self.session}: {request.company_name} ({request.ticker}) as of "
                f"{request.valuation_date}"
            )
        self.show_answer()

    def resume(self, session: str) -> None:
        known = sessions(self.conn)
        if not session:
            if not known:
                self.console.print("No saved sessions.")
                return
            table = Table("Session", "Company", "Date", "Status")
            for item in known[:15]:
                values = dict(self.graph.get_state(thread(item)).values)
                request = values.get("request")
                route = values.get("route")
                name = (
                    f"{request.company_name} ({request.ticker})"
                    if request
                    else (route.text if route else "")
                )
                when = str(request.valuation_date) if request else ""
                table.add_row(item, name, when, str(values.get("status", "")))
            self.console.print(table)
            return
        if session not in known:
            self.console.print(f"No saved session {session}.")
            return
        self.session = session
        self.show_session()


app = typer.Typer(add_completion=False, help="Chat with the valuation agent.")


@app.command()
def chat(
    session: Annotated[str | None, typer.Option(help="Resume a saved session")] = None,
    today: Annotated[str | None, typer.Option(help="Reference date (YYYY-MM-DD)")] = None,
    offline: Annotated[bool, typer.Option(help="Replay saved responses only")] = False,
    mock_model: Annotated[bool, typer.Option(help="Run without Ollama (rules only)")] = False,
    checkpoints: Annotated[Path, typer.Option(help="Session database")] = Path(
        "data/state/checkpoints.sqlite"
    ),
) -> None:
    """Start the chat."""
    from dcf_agent.config import load_config
    from dcf_agent.graph.services import LiveServices
    from dcf_agent.graph.workflow import build_graph, open_checkpointer

    config = load_config()
    config = config.model_copy(
        update={"settings": config.settings.model_copy(update={"mock_llm": mock_model})}
    )
    console = Console()
    reference = date.fromisoformat(today) if today else date.today()
    saver = open_checkpointer(checkpoints)
    try:
        with LiveServices(config, reference, offline=offline) as live:
            graph = build_graph(live.services(), saver)
            Chat(graph, console, console.input, saver.conn, session).run()
    finally:
        saver.conn.close()


if __name__ == "__main__":
    app()
