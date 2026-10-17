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
from rich.markup import escape
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
    "normalize": "Reading the filings for one-off items",
    "adjustment_review": "Adjustments review",
    "forecast": "Building bear, base and bull scenarios",
    "assumption_review": "Assumptions review",
    "value": "Valuing the scenarios",
    "excel": "Writing the workbook",
    "wacc": "Computing the cost of capital",
    "unwired": "Skipping a step not available yet",
    "answer": "Writing the answer",
    "halt": "Budget check",
}
Ask = Callable[[str], str]


def parse_signoff(answer: str) -> dict[str, Any]:
    word, _, rest = answer.strip().partition(" ")
    word = word.lower()
    if word == "stop":
        return {"action": "stop"}
    if word == "back":
        return {"action": "back", "note": rest.strip() or None}
    return {"action": "approve"}


def football_text(ranges: list[dict[str, Any]], price: float | None, width: int = 40) -> str:
    """The football field as text bars on one scale, the price marked with '|'."""
    if not ranges:
        return ""
    values = [v for r in ranges for v in (r["low"], r["high"])] + ([price] if price else [])
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0

    def at(v: float) -> int:
        return int((v - lo) / span * (width - 1) + 0.5)

    lines = []
    for r in ranges:
        bar = [" "] * width
        for k in range(at(r["low"]), at(r["high"]) + 1):
            bar[k] = "="
        if price:
            bar[at(price)] = "|"
        label = escape(r["label"][:34])
        lines.append(f"{label:34} {''.join(bar)} {r['low']:,.2f}-{r['high']:,.2f}")
    if price:
        lines.append(f"{'':34} {' ' * at(price)}^ price {price:,.2f}")
    return "\n".join(lines)


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
            decision = self.decide(gate)
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
                    if node not in (
                        "gate0",
                        "data_review",
                        "adjustment_review",
                        "assumption_review",
                    ):
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
            "Enter to confirm; or 'stop', 'date YYYY-MM-DD', 'ticker XYZ', 'industry <label>', "
            "'autopilot' (later gates approve themselves, listed as unreviewed): "
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
        if word == "autopilot":
            decision["autopilot"] = True
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

    def decide(self, gate: dict[str, Any]) -> dict[str, Any]:
        if gate["gate"] == 0:
            return self.gate0(gate)
        if gate.get("kind") == "adjustments":
            return self.adjustments(gate)
        if gate.get("kind") == "assumptions":
            return self.assumptions(gate)
        if gate.get("kind") == "signoff":
            return self.signoff(gate)
        return self.gate1(gate)

    def signoff(self, gate: dict[str, Any]) -> dict[str, Any]:
        """Gate 3: approve and export, back to the assumptions, or stop."""
        self.show_signoff(gate)
        answer = self.ask(
            "Enter to approve and export; or 'back <note>' (to the assumptions), 'stop': "
        ).strip()
        return parse_signoff(answer)

    def adjustments(self, gate: dict[str, Any]) -> dict[str, Any]:
        """Gate 1 on proposed EBIT adjustments: approve all, decide each, or stop."""
        self.show_adjustments(gate)
        items = gate["items"]
        if not items:
            answer = self.ask("Enter to continue, or 'stop': ").strip().lower()
            return {"action": "stop" if answer == "stop" else "approve_all"}
        answer = self.ask("a) approve all  d) decide each  s) stop [a]: ").strip().lower()
        if answer in ("s", "stop"):
            return {"action": "stop"}
        if answer not in ("d", "decide"):
            return {"action": "approve_all"}
        approve: list[str] = []
        reject: list[str] = []
        edit: dict[str, float] = {}
        for n, item in enumerate(items, 1):
            while True:
                reply = (
                    self.ask(
                        f"{n}. {item['item']} {item['period_end']}: approve, reject, or "
                        "'edit <amount in $M>' [approve]: "
                    )
                    .strip()
                    .lower()
                )
                word, _, rest = reply.partition(" ")
                if word in ("", "a", "approve"):
                    approve.append(item["id"])
                elif word in ("r", "reject"):
                    reject.append(item["id"])
                elif word in ("e", "edit"):
                    try:
                        amount = float(rest.replace(",", "")) * 1e6
                    except ValueError:
                        self.console.print("Give the amount in millions, e.g. 'edit 250'.")
                        continue
                    if amount < 0:
                        self.console.print("The amount is a size; the direction stays.")
                        continue
                    edit[item["id"]] = amount
                else:
                    continue
                break
        return {"action": "decide", "approve": approve, "reject": reject, "edit": edit}

    def assumptions(self, gate: dict[str, Any]) -> dict[str, Any]:
        """Gate 2: approve, edit a cell, ask why, re-think a scenario, or stop."""
        self.show_assumptions(gate)
        while True:
            answer = self.ask(
                "Enter to approve; or 'edit base margin 2028 25%' (add '! reason' to "
                "override a hard bound), 'why <question>', 'rethink bear <note>', 'stop': "
            ).strip()
            decision = parse_assumption_command(answer, gate)
            if decision is not None:
                return decision
            self.console.print(
                "Not understood. Fields: growth, margin, sales, tax, terminal, roic, probability."
            )

    def gate1(self, gate: dict[str, Any]) -> dict[str, Any]:
        self.console.print(
            Panel(gate["question"] or "", title="Data review", border_style="yellow")
        )
        answer = self.ask("continue or stop? [continue]: ").strip().lower()
        return {"action": "stop" if answer == "stop" else "continue"}

    # Display ----------------------------------------------------------------------------

    def show_assumptions(self, gate: dict[str, Any]) -> None:
        if gate.get("message"):
            self.console.print(f"[yellow]{escape(gate['message'])}[/]")
        if gate.get("answer"):
            self.console.print(Panel(escape(gate["answer"]), title="Why", border_style="cyan"))
        first = gate["scenarios"][0]
        years = first["years"]
        table = Table(
            "Scenario",
            "Prob.",
            *[f"Growth {y}" for y in years],
            *[f"Margin {y}" for y in years],
            "Sales/IC",
            "Tax",
            "g∞",
            "ROIC∞",
            "Value",
        )
        for s in gate["scenarios"]:
            value = s["value_per_share"]
            table.add_row(
                s["scenario"],
                f"{s['probability']:.0%}",
                *[f"{v:.1%}" for v in s["revenue_growth"]],
                *[f"{v:.1%}" for v in s["operating_margin"]],
                f"{s['sales_to_capital'][0]:.2f}",
                f"{s['tax_rate'][0]:.0%}->{s['tax_rate'][-1]:.0%}",
                f"{s['terminal_growth']:.2%}",
                f"{s['terminal_roic']:.1%}",
                "-" if value is None else f"{value:,.2f}",
            )
        self.console.print(Panel(table, title="Gate 2: assumptions", border_style="yellow"))
        w = gate["wacc"]
        weighted = gate["weighted"]
        self.console.print(
            f"WACC {w['wacc']:.2%} (risk-free {w['risk_free']:.2%}, ERP {w['erp']:.2%}, beta "
            f"{w['beta']:.2f} {w['beta_method']}, cost of equity {w['cost_of_equity']:.2%}, "
            f"debt {w['pretax_cost_of_debt']:.2%} pre-tax, {w['weight_equity']:.0%} equity); "
            f"terminal WACC {w['terminal_wacc']:.2%}."
            + (f" Weighted value {weighted:,.2f}" if weighted is not None else "")
            + (f"; market price {gate['price']:,.2f} (context only)." if gate["price"] else ".")
        )
        for s in gate["scenarios"]:
            self.console.print(f"[bold]{s['scenario']}[/]: {escape(s['narrative'])}")
            for flag in s["flags"]:
                self.console.print(f"   [yellow]check:[/] {escape(flag)}")
        for item in gate["review"]:
            self.console.print(f"[yellow]For review:[/] {escape(item)}")

    def show_signoff(self, gate: dict[str, Any]) -> None:
        gap = gate["gap"]
        self.console.print(
            Panel(
                f"Value per share [bold]{gate['weighted']:,.2f}[/] (bear {gate['low']:,.2f} to "
                f"bull {gate['high']:,.2f})"
                + (
                    f"; price {gate['price']:,.2f} ({gap:+.0%})"
                    if gate["price"] and gap is not None
                    else ""
                ),
                title=f"Gate 3: sign-off, {escape(gate['company'])} ({gate['ticker']})",
                border_style="yellow",
            )
        )
        weights = ", ".join(
            f"{s['scenario']} {s['probability']:.0%} {s['value_per_share']:,.2f}"
            for s in gate["scenarios"]
        )
        self.console.print(f"Scenarios: {weights}")
        self.console.print(football_text(gate["ranges"], gate["price"]))
        for d in gate["drivers"]:
            self.console.print(
                f"Driver {escape(d['driver'])} ({escape(d['step'])}): "
                f"{min(d['low'], d['high']):,.2f} to {max(d['low'], d['high']):,.2f}"
            )
        critic = gate["critic"]
        if critic is not None:
            self.console.print(
                f"Critic: score {critic['score']:.1f}, "
                f"{'passed' if critic['passed'] else '[red]not passed[/]'}, "
                f"{critic['loops']} loops"
            )
            for issue in critic["issues"]:
                color = "red" if issue["severity"] == "high" else "yellow"
                self.console.print(
                    f"   [{color}]{issue['severity']}[/] {escape(issue['description'])} "
                    f"(fix: {escape(issue['fix'])})"
                )
        for change in gate["changes"]:
            self.console.print(f"   Critic change: {escape(change)}")
        for item in gate["unreviewed"]:
            self.console.print(f"[yellow]Unreviewed:[/] {escape(item)}")

    def show_adjustments(self, gate: dict[str, Any]) -> None:
        table = Table("#", "Period", "Item", "EBIT", "% of EBIT", "Confidence")
        for n, item in enumerate(gate["items"], 1):
            sign = "+" if item["direction"] == "increase" else "-"
            share = f"{item['pct_ebit']:.1%}" if item["pct_ebit"] is not None else ""
            flag = " [yellow](ambiguous)[/]" if item["ambiguous"] else ""
            table.add_row(
                str(n),
                item["period_end"],
                item["item"],
                f"{sign}${item['amount'] / 1e6:,.0f}M",
                share,
                item["confidence"] + flag,
            )
        self.console.print(
            Panel(table, title="Gate 1: proposed EBIT adjustments", border_style="yellow")
        )
        for n, item in enumerate(gate["items"], 1):
            self.console.print(f"[bold]{n}.[/] {escape(item['rationale'])}")
            if item["passage"]:
                self.console.print(f"   [dim]Filing: {escape(item['passage'][:300])}[/]")
        if gate["auto_approved"]:
            self.console.print(
                f"{len(gate['auto_approved'])} small adjustments approved by policy: "
                + ", ".join(f"{i['item']} {i['period_end']}" for i in gate["auto_approved"])
            )
        for flag in gate["flags"]:
            self.console.print(
                f"[yellow]Kept in EBIT, check:[/] {escape(flag['item'])}: "
                f"{escape(flag['rationale'])}"
            )
        for reading in gate["readings"]:
            self.console.print(f"[yellow]Filing reading to check:[/] {escape(reading)}")

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
            self.console.print(escape(values["answer"]))
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
        if values.get("proposals") is not None:
            table.add_row(
                "Adjustments",
                f"{len(values.get('approved_adjustments', ()))} approved of "
                f"{len(values['proposals'])} proposed"
                + ("" if values.get("adjustments_decided") else " [yellow](awaiting Gate 1)[/]"),
            )
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
            decision = self.decide(gate)
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


FIELDS = {
    "growth": "revenue_growth",
    "revenue": "revenue_growth",
    "margin": "operating_margin",
    "sales": "sales_to_capital",
    "s2c": "sales_to_capital",
    "tax": "tax_rate",
    "terminal": "terminal_growth",
    "g": "terminal_growth",
    "roic": "terminal_roic",
    "probability": "probability",
    "prob": "probability",
}


def parse_assumption_command(text: str, gate: dict[str, Any]) -> dict[str, Any] | None:
    """'' -> approve; 'edit base margin 2028 25% ! reason'; 'why ...'; 'rethink bear ...'."""
    if not text:
        return {"action": "approve"}
    word, _, rest = text.partition(" ")
    word = word.lower()
    if word == "stop":
        return {"action": "stop"}
    if word == "why":
        return {"action": "why", "question": rest.strip() or "why these assumptions?"}
    if word == "rethink":
        name, _, note = rest.strip().partition(" ")
        if name.lower() not in ("bear", "base", "bull"):
            return None
        return {"action": "rethink", "scenario": name.lower(), "note": note.strip() or None}
    if word != "edit":
        return None
    command, _, reason = rest.partition("!")
    parts = command.split()
    if len(parts) < 3 or parts[0].lower() not in ("bear", "base", "bull"):
        return None
    field = FIELDS.get(parts[1].lower())
    if field is None:
        return None
    year: int | None = None
    if len(parts) == 4:
        first = gate["scenarios"][0]["years"][0]
        token = parts[2].lower().removeprefix("y")
        if not token.isdigit():
            return None
        number = int(token)
        year = number - first + 1 if number >= 1900 else number
        if year < 1:
            return None
    raw = parts[-1].replace(",", "")
    try:
        value = float(raw.rstrip("%"))
    except ValueError:
        return None
    if raw.endswith("%") or (field != "sales_to_capital" and value > 1):
        value /= 100
    edit: dict[str, Any] = {"scenario": parts[0].lower(), "field": field, "value": value}
    if year is not None:
        edit["year"] = year
    decision: dict[str, Any] = {"action": "edit", "edits": [edit]}
    if reason.strip():
        decision.update({"override": True, "reason": reason.strip()})
    return decision


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
