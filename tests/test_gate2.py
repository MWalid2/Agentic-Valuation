"""Gate 2: approve, edit (bounds and override), why, re-think, stop, resume; chat commands."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_router import router
from test_valuation_graph import BUNDLE, bundle

from dcf_agent.agents.forecast import ForecastAgent
from dcf_agent.chat import parse_assumption_command
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


def services(notes: list[str | None] | None = None) -> Services:
    data = bundle()

    def forecast(b, normalized, wacc, note=None):
        if notes is not None:
            notes.append(note)
        result = ForecastAgent(None).run(b, normalized, wacc, assemble(b, marginal_tax=0.25))
        return result.scenarios, tuple(result.notes)

    def collect(request, industry):
        return data.model_copy(update={"request": request}), None

    return Services(
        router=router(),
        collect=collect,
        wacc=lambda b: (compute_wacc(assemble(b, marginal_tax=0.25).wacc_inputs), None),
        forecast=forecast,
        now=lambda: datetime(2026, 10, 15, 12, tzinfo=UTC),
    )


def to_gate2(tmp_path: Path, notes=None, session: str = "g2"):
    saver = open_checkpointer(tmp_path / "c.sqlite")
    graph = build_graph(services(notes), saver)
    data = bundle()
    start(graph, session, f"{data.request.ticker}, {data.request.valuation_date}")
    shown = resume(graph, session, {"action": "confirm", "output": "quick"})
    return graph, saver, shown["__interrupt__"][0].value


def base(gate: dict) -> dict:
    return next(s for s in gate["scenarios"] if s["scenario"] == "base")


def test_gate2_shows_scenarios_wacc_and_values_then_approval_values_them(tmp_path: Path):
    graph, saver, gate = to_gate2(tmp_path)
    assert gate["gate"] == 2 and [s["scenario"] for s in gate["scenarios"]] == [
        "bear",
        "base",
        "bull",
    ]
    assert base(gate)["value_per_share"] is not None and gate["wacc"]["wacc"] > 0
    final = resume(graph, "g2", {"action": "approve"})
    assert final["status"] == "done" and final["valuation"].weighted > 0
    saver.conn.close()


def test_an_edit_in_bounds_is_applied_recorded_and_shown_again(tmp_path: Path):
    graph, saver, gate = to_gate2(tmp_path)
    before = base(gate)["value_per_share"]
    edit = {"scenario": "base", "field": "operating_margin", "value": 0.35}
    again = resume(graph, "g2", {"action": "edit", "edits": [edit]})["__interrupt__"][0].value
    assert again["message"].startswith("Edited: base.operating_margin=0.35")
    assert base(again)["operating_margin"] == [0.35, 0.35, 0.35]
    assert base(again)["value_per_share"] > before
    final = resume(graph, "g2", {"action": "approve"})
    record = [a for a in final["approvals"] if a.gate == 2]
    assert [a.decision for a in record] == ["edited", "approved"]
    assert record[0].edit_refs == ("base.operating_margin=0.35",) and not record[0].bounds_override
    base_scenario = next(s for s in final["forecast"].scenarios if s.scenario == "base")
    assert base_scenario.operating_margin[4].prov.source == "user_override"
    saver.conn.close()


def test_a_hard_bound_edit_needs_an_override_with_a_reason(tmp_path: Path):
    graph, saver, gate = to_gate2(tmp_path)
    # Above the 4% long-run cap (a policy bound), below the risk-free rate (the engine's own).
    edit = {"scenario": "bull", "field": "terminal_growth", "value": 0.045}
    refused = resume(graph, "g2", {"action": "edit", "edits": [edit]})["__interrupt__"][0].value
    assert refused["message"].startswith("Edit refused, it breaks a hard bound")
    bull = next(s for s in refused["scenarios"] if s["scenario"] == "bull")
    assert bull["terminal_growth"] < 0.045  # unchanged
    accepted = resume(
        graph,
        "g2",
        {"action": "edit", "edits": [edit], "override": True, "reason": "inflation regime"},
    )["__interrupt__"][0].value
    assert "hard bound overridden: inflation regime" in accepted["message"]
    final = resume(graph, "g2", {"action": "approve"})
    override = next(a for a in final["approvals"] if a.gate == 2 and a.decision == "edited")
    assert override.bounds_override and override.override_reason == "inflation regime"
    saver.conn.close()


def test_an_edit_the_engine_cannot_value_is_refused_even_with_an_override(tmp_path: Path):
    graph, saver, _ = to_gate2(tmp_path)
    edit = {"scenario": "bull", "field": "terminal_growth", "value": 0.07}  # above risk-free
    refused = resume(
        graph, "g2", {"action": "edit", "edits": [edit], "override": True, "reason": "test"}
    )["__interrupt__"][0].value
    assert refused["message"] == "Edit refused: the engine cannot value bull with it"
    saver.conn.close()


def test_why_rethink_and_stop(tmp_path: Path):
    notes: list[str | None] = []
    graph, saver, _ = to_gate2(tmp_path, notes)
    why = resume(graph, "g2", {"action": "why", "question": "why this margin?"})
    assert "Margin" in why["__interrupt__"][0].value["answer"]
    redo = resume(
        graph, "g2", {"action": "rethink", "scenario": "bear", "note": "assume a price war"}
    )
    assert "bear re-thought" in redo["__interrupt__"][0].value["message"]
    assert notes[-1] == "assume a price war"  # the note reaches the Forecast Agent
    final = resume(graph, "g2", {"action": "stop"})
    assert final["status"] == "stopped" and final["approvals"][-1].gate == 2
    assert "valuation" not in final
    saver.conn.close()


def test_a_session_waiting_at_gate2_survives_a_restart(tmp_path: Path):
    _, saver, gate = to_gate2(tmp_path)
    saver.conn.close()
    saver = open_checkpointer(tmp_path / "c.sqlite")
    graph = build_graph(services(), saver)
    assert pending_gate(graph, "g2") == gate
    final = resume(graph, "g2", {"action": "approve"})
    assert final["valuation"].weighted > 0
    saver.conn.close()


def test_chat_commands_become_gate2_decisions():
    gate = {"scenarios": [{"years": [2026, 2030, 2035]}]}
    assert parse_assumption_command("", gate) == {"action": "approve"}
    edit = parse_assumption_command("edit base margin 2028 25%", gate)
    assert edit == {
        "action": "edit",
        "edits": [{"scenario": "base", "field": "operating_margin", "value": 0.25, "year": 3}],
    }
    over = parse_assumption_command("edit bull terminal 5.5% ! inflation regime", gate)
    assert over is not None and over["override"] and over["reason"] == "inflation regime"
    assert over["edits"][0]["value"] == pytest.approx(0.055)
    assert parse_assumption_command("edit base sales 1.8", gate)["edits"][0]["value"] == 1.8  # type: ignore[index]
    assert parse_assumption_command("rethink bear assume a price war", gate) == {
        "action": "rethink",
        "scenario": "bear",
        "note": "assume a price war",
    }
    assert parse_assumption_command("edit base nonsense 3%", gate) is None


def test_a_change_reruns_only_what_depends_on_the_forecast(tmp_path: Path):
    from dcf_agent.graph.workflow import stale_after

    assert stale_after("forecast") == {"value", "comps", "critic", "excel", "report"}
    assert stale_after("wacc") == {"forecast", "value", "comps", "critic", "excel", "report"}
    calls: list[str | None] = []
    graph, saver, _ = to_gate2(tmp_path, calls)
    first = resume(graph, "g2", {"action": "approve"})
    before, events = first["valuation"].weighted, len(first["audit_log"])
    forecasts = len(calls)
    changed = start(graph, "g2", "change the base margin in 2028 to 35%")
    assert changed["status"] == "done" and changed["valuation"].weighted > before
    assert len(calls) == forecasts  # the forecast was not rebuilt
    new = changed["audit_log"][events:]
    assert {e.component for e in new} <= {
        "router",
        "forecast",
        "engine",
        "comps",
        "critic",
        "system",
    }
    assert not any(e.component == "data" for e in new)  # nothing collected again
    assert "Changed: base.operating_margin[year 3]=0.35" in changed["answer"]
    record = changed["approvals"][-1]
    assert record.gate == 2 and record.decision == "edited"
    refused = start(graph, "g2", "set the bull terminal growth to 4.5%")
    assert "breaks a hard bound" in refused["answer"]
    saver.conn.close()


def test_change_requests_in_plain_language():
    from dcf_agent.agents.assumptions import parse_change

    edits, reason = parse_change("change the base margin in 2028 to 25%", 2026) or ([], None)
    assert [(e.scenario, e.field, e.value, e.year) for e in edits] == [
        ("base", "operating_margin", 0.25, 3)
    ]
    edits, reason = parse_change("set bull terminal growth to 2.5% ! new regime", 2026) or (
        [],
        None,
    )
    assert [(e.field, e.value, e.year) for e in edits] == [("terminal_growth", 0.025, None)]
    assert reason == "new regime"
    assert parse_change("make it better", 2026) is None


def test_the_chat_shows_bracketed_text_as_written():
    import io

    from rich.console import Console

    from dcf_agent.chat import Chat

    console = Console(record=True, width=160, file=io.StringIO())
    chat = Chat(None, console, lambda _: "", None)  # type: ignore[arg-type]
    scenario = {
        "scenario": "base",
        "probability": 0.5,
        "years": [2026],
        "revenue_growth": [0.05],
        "operating_margin": [0.3],
        "sales_to_capital": [1.5],
        "tax_rate": [0.2, 0.25],
        "terminal_growth": 0.03,
        "terminal_roic": 0.1,
        "value_per_share": 50.0,
        "narrative": "Base case.",
        "rationale": {},
        "flags": [],
    }
    wacc = {
        k: 0.08
        for k in (
            "risk_free",
            "erp",
            "beta",
            "cost_of_equity",
            "pretax_cost_of_debt",
            "tax_rate",
            "weight_equity",
            "wacc",
            "terminal_wacc",
        )
    }
    chat.show_assumptions(
        {
            "message": "Edited: base.operating_margin[year 3]=0.3",
            "answer": None,
            "scenarios": [scenario],
            "wacc": {**wacc, "beta_method": "bottom_up"},
            "weighted": 50.0,
            "price": 80.0,
            "review": [],
        }
    )
    assert "base.operating_margin[year 3]=0.3" in console.export_text()
