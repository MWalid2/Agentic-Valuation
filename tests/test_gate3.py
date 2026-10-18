"""Gate 3: sign-off and export, back to Gate 2, stop; autopilot and unreviewed items."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_router import router
from test_valuation_graph import BUNDLE, bundle

from dcf_agent.agents.forecast import ForecastAgent
from dcf_agent.chat import football_text, parse_signoff
from dcf_agent.engine.assemble import assemble
from dcf_agent.engine.wacc import compute_wacc
from dcf_agent.graph.workflow import (
    Services,
    build_graph,
    open_checkpointer,
    pending_gate,
    resume,
    start,
)

pytestmark = pytest.mark.skipif(not BUNDLE.exists(), reason="reference bundle not collected")


def graph_for(tmp_path: Path):
    data = bundle()

    def forecast(b, normalized, wacc, note=None):
        result = ForecastAgent(None).run(b, normalized, wacc, assemble(b, marginal_tax=0.25))
        return result.scenarios, tuple(result.notes)

    services = Services(
        router=router(),
        collect=lambda request, industry: (data.model_copy(update={"request": request}), None),
        wacc=lambda b: (compute_wacc(assemble(b, marginal_tax=0.25).wacc_inputs), None),
        forecast=forecast,
        risks=lambda b: [("accession 0000021344-26-000001", "Demand could fall. More text.")],
        models=tmp_path / "models",
        reports=tmp_path / "reports",
        now=lambda: datetime(2026, 10, 15, 12, tzinfo=UTC),
    )
    saver = open_checkpointer(tmp_path / "c.sqlite")
    return build_graph(services, saver), saver, data


def to_gate3(graph, data, session="g3", autopilot=False):
    start(graph, session, f"{data.request.ticker}, {data.request.valuation_date}")
    state = resume(graph, session, {"action": "confirm", "output": "excel", "autopilot": autopilot})
    if autopilot:
        return state
    assert state["__interrupt__"][0].value["kind"] == "assumptions"
    return resume(graph, session, {"action": "approve"})


def test_gate3_shows_the_sign_off_and_approval_exports_the_report(tmp_path: Path):
    graph, saver, data = graph_for(tmp_path)
    shown = to_gate3(graph, data)["__interrupt__"][0].value
    assert pending_gate(graph, "g3") == shown  # what the chat asks on
    assert shown["gate"] == 3 and shown["kind"] == "signoff"
    assert [s["scenario"] for s in shown["scenarios"]] == ["bear", "base", "bull"]
    assert shown["ranges"][0]["label"].startswith("DCF") and len(shown["drivers"]) == 3
    assert shown["critic"]["passed"] and shown["actions"] == ["approve", "back", "stop"]
    final = resume(graph, "g3", {"action": "approve"})
    assert final["status"] == "done" and final["done"][-2:] == ("report", "answer")
    record = final["approvals"][-1]
    assert record.gate == 3 and record.decision == "approved" and not record.autopilot
    folder = Path(final["report_folder"])
    for name in (
        "memo.md",
        "memo.html",
        "football-field.svg",
        "summary.json",
        "model.xlsx",
        "audit-log.json",
        "provenance-audit.json",
    ):
        assert (folder / name).exists(), name
    log = json.loads((folder / "audit-log.json").read_text(encoding="utf-8"))
    assert [a["gate"] for a in log["approvals"]][-1] == 3
    assert json.loads((folder / "provenance-audit.json").read_text(encoding="utf-8"))["clean"]
    memo = (folder / "memo.md").read_text(encoding="utf-8")
    assert "Demand could fall. (10-K risk factors)" in memo
    assert "Unreviewed items" not in memo
    saver.conn.close()


def test_back_returns_to_gate2_then_signs_off_again(tmp_path: Path):
    graph, saver, data = graph_for(tmp_path)
    to_gate3(graph, data)
    again = resume(graph, "g3", {"action": "back", "note": "margins look high"})
    gate = again["__interrupt__"][0].value
    assert gate["kind"] == "assumptions" and "margins look high" in gate["message"]
    edit = {"scenario": "base", "field": "operating_margin", "value": 0.25}
    resume(graph, "g3", {"action": "edit", "edits": [edit]})
    signoff = resume(graph, "g3", {"action": "approve"})["__interrupt__"][0].value
    assert signoff["kind"] == "signoff"
    final = resume(graph, "g3", {"action": "approve"})
    assert final["status"] == "done"
    assert [a.decision for a in final["approvals"] if a.gate == 3] == ["rejected", "approved"]
    saver.conn.close()


def test_stop_at_gate3_exports_nothing(tmp_path: Path):
    graph, saver, data = graph_for(tmp_path)
    to_gate3(graph, data)
    final = resume(graph, "g3", {"action": "stop"})
    assert final["status"] == "stopped" and not final.get("report_folder")
    assert not (tmp_path / "reports").exists()
    saver.conn.close()


def test_autopilot_approves_every_gate_and_lists_what_it_passed(tmp_path: Path):
    graph, saver, data = graph_for(tmp_path)
    final = to_gate3(graph, data, session="auto", autopilot=True)
    assert final["status"] == "done" and pending_gate(graph, "auto") is None
    later = [a for a in final["approvals"] if a.gate in (2, 3)]
    assert later and all(a.autopilot and a.actor == "policy" for a in later)
    assert any(u.startswith("Assumptions approved without review") for u in final["unreviewed"])
    memo = (Path(final["report_folder"]) / "memo.md").read_text(encoding="utf-8")
    assert "## Unreviewed items (autopilot)" in memo and "Assumptions approved" in memo
    saver.conn.close()


def test_chat_sign_off_commands_and_text_football_field():
    assert parse_signoff("") == {"action": "approve"}
    assert parse_signoff("back lower the margin") == {"action": "back", "note": "lower the margin"}
    assert parse_signoff("stop") == {"action": "stop"}
    text = football_text(
        [
            {"label": "DCF", "low": 30, "high": 60, "mark": 45},
            {"label": "Comps", "low": 40, "high": 55, "mark": None},
        ],
        50.0,
    )
    assert "30.00-60.00" in text and "^ price 50.00" in text and "|" in text


def chat_run(tmp_path: Path, graph, saver, *answers: str) -> str:
    import io

    from rich.console import Console
    from test_chat import Script

    from dcf_agent.chat import Chat

    console = Console(record=True, width=160, file=io.StringIO())
    Chat(graph, console, Script(*answers), saver.conn).run()
    saver.conn.close()
    return console.export_text()


def test_a_chat_session_signs_off_and_lists_the_report_assumptions_and_trace(tmp_path: Path):
    graph, saver, data = graph_for(tmp_path)
    out = chat_run(
        tmp_path,
        graph,
        saver,
        f"{data.request.ticker}, {data.request.valuation_date}",
        "",  # confirm at Gate 0
        "3",  # the full model
        "",  # approve at Gate 2
        "",  # approve and export at Gate 3
        "/assumptions",
        "/report",
        "/trace",
        "/quit",
    )
    assert "done Valuing the scenarios" in out and "Weighted value per share" in out
    assert "done Reviewing the valuation (Critic)" in out and ": Score" in out
    assert "warning Comps not available in this session" in out
    assert "Gate 3: sign-off" in out and "done Sign-off and report" in out
    assert "Report folder:" in out and "memo.md" in out and "model.xlsx" in out
    assert "Scenario" in out and "Growth y1" in out
    assert "Trace written:" in out
    assert "Not completed" not in out


def test_a_failed_value_says_what_happened_and_what_to_try(tmp_path: Path):
    from test_critic import growth_above_rf

    from dcf_agent.agents.forecast import ForecastAgent

    graph, saver, data = graph_for(tmp_path)

    saver.conn.close()
    from dcf_agent.graph.workflow import Services

    def flawed(b, normalized, wacc, note=None):
        result = ForecastAgent(None).run(b, normalized, wacc, assemble(b, marginal_tax=0.25))
        return growth_above_rf({"forecast": result.scenarios, "wacc": wacc})["forecast"], ()

    bad = Services(
        router=router(),
        collect=lambda request, industry: (data.model_copy(update={"request": request}), None),
        wacc=lambda b: (compute_wacc(assemble(b, marginal_tax=0.25).wacc_inputs), None),
        forecast=flawed,
        now=lambda: datetime(2026, 10, 15, 12, tzinfo=UTC),
    )
    saver = open_checkpointer(tmp_path / "bad.sqlite")
    graph = build_graph(bad, saver)
    out = chat_run(
        tmp_path,
        graph,
        saver,
        f"{data.request.ticker}, {data.request.valuation_date}",
        "",
        "2",  # a quick value
        "",  # approve at Gate 2
        "/quit",
    )
    assert "Not completed" in out and "What happened:" in out and "Not valued" in out
    assert "edit them at Gate 2" in out
