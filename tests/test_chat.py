"""Scripted terminal sessions: gates, progress, answers, follow-ups, commands and resume."""

import io
from pathlib import Path

from rich.console import Console
from test_graph import NOW, Calls, bundle
from test_router import router
from test_wacc import inputs

from dcf_agent.agents.data_loop import Blocker, DataReview
from dcf_agent.chat import Chat, sessions
from dcf_agent.engine.wacc import compute_wacc
from dcf_agent.graph.workflow import Services, build_graph, open_checkpointer, start


class Script:
    """Answers prompts in order; records them; ends the session when it runs out."""

    def __init__(self, *answers: str) -> None:
        self.answers, self.prompts = list(answers), []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def services(calls: Calls, collect=None) -> Services:
    return Services(
        router=router(),
        collect=collect or calls.collect,
        wacc=lambda _: (compute_wacc(inputs()), None),
        now=lambda: NOW,
    )


def chat(tmp_path: Path, script: Script, calls: Calls, session=None, collect=None):
    saver = open_checkpointer(tmp_path / "chat.sqlite")
    console = Console(record=True, width=140, file=io.StringIO())
    graph = build_graph(services(calls, collect), saver)
    session_chat = Chat(graph, console, script, saver.conn, session)
    session_chat.run()
    saver.conn.close()
    return session_chat, console.export_text()


def test_session_with_gate_progress_answer_sources_and_a_follow_up(tmp_path: Path):
    calls = Calls()
    script = Script(
        "Apple, 2026-10-11, what is the cost of capital?",
        "",  # confirm at Gate 0
        "/sources",
        "where does the WACC come from?",
        "/state",
        "/quit",
    )
    session, out = chat(tmp_path, script, calls)
    assert "Gate 0: confirm" in out and "Apple Inc. (AAPL)" in out
    assert "Collecting SEC filings" in out and "Computing the cost of capital" in out
    assert "cost of capital as of 2026-10-11" in out and "Beta method chosen by" in out
    assert "Reference" in out  # the /sources table
    assert "where the numbers come from" in out  # the follow-up answer (I10)
    assert calls.collected == [("AAPL", None)]  # the follow-up reused the data
    assert "Session state" in out and "Audit events" in out
    # The follow-up is its own run: followup and answer only (data and WACC reused).
    assert "2/30 steps" in out
    assert f"Session saved: {session.session}" in out


def test_ambiguous_company_then_the_output_menu(tmp_path: Path):
    calls = Calls()
    script = Script("Johnson, 2026-10-11", "JNJ", "", "1", "latest revenue?", "/quit")
    _, out = chat(tmp_path, script, calls)
    assert "Which company?" in script.prompts[1] and "JCI" in script.prompts[1]
    assert "1) ask a question" in script.prompts[3]
    assert calls.collected == [("JNJ", None)]
    assert "reported figures as of 2026-10-11" in out


def test_stop_at_gate0_then_a_new_company_starts_a_new_session(tmp_path: Path):
    calls = Calls()
    script = Script("Apple, 2026-10-11", "stop", "KO, 2026-10-11, latest cash?", "", "/quit")
    _, out = chat(tmp_path, script, calls)
    assert "Session stopped" in out
    assert calls.collected == [("KO", None)]


def test_resume_a_finished_session_and_ask_without_collecting_again(tmp_path: Path):
    calls = Calls()
    first, _ = chat(tmp_path, Script("Apple, 2026-10-11, what WACC?", "", "/quit"), calls)
    script = Script("/resume", f"/resume {first.session}", "which beta should we use?", "/quit")
    _, out = chat(tmp_path, script, calls)
    assert first.session in out and "Apple Inc. (AAPL)" in out  # the session list
    assert "cost of capital as of 2026-10-11" in out
    assert calls.collected == [("AAPL", None)]  # data came from the checkpoint


def test_resume_a_session_waiting_at_gate0(tmp_path: Path):
    calls = Calls()
    saver = open_checkpointer(tmp_path / "chat.sqlite")
    start(build_graph(services(calls), saver), "waiting", "KO, 2026-10-11, latest cash?")
    assert sessions(saver.conn) == ["waiting"]
    saver.conn.close()
    _, out = chat(tmp_path, Script("", "/quit"), calls, session="waiting")
    assert "waiting at Gate 0" in out
    assert calls.collected == [("KO", None)] and "reported figures" in out


def test_data_review_gate_continues(tmp_path: Path):
    blocker = Blocker(consumer="wacc", key="ttm", reason="captive_finance_debt (unresolved)")
    review = DataReview(status="ask_user", remaining=(blocker,), question="Blocked; continue?")
    calls = Calls()

    def collect(request, industry):
        calls.collected.append((request.ticker, industry))
        return bundle(request), review

    script = Script("Apple, 2026-10-11, what WACC?", "", "continue", "/quit")
    _, out = chat(tmp_path, script, calls, collect=collect)
    assert "Data review" in out and "Blocked; continue?" in out
    assert "cost of capital" in out
