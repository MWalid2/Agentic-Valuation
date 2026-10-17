"""Quick value in the graph and its answers, on a saved reference bundle (skipped without it)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_router import router

from dcf_agent.agents.answer import render
from dcf_agent.agents.forecast import ForecastAgent
from dcf_agent.contracts.external import DataBundle
from dcf_agent.engine.assemble import assemble
from dcf_agent.engine.normalize import Policy, normalize
from dcf_agent.engine.valuation import value_scenarios
from dcf_agent.engine.wacc import compute_wacc
from dcf_agent.graph.workflow import Services, build_graph, open_checkpointer, resume, start

BUNDLE = Path(__file__).resolve().parents[1] / "outputs/insights-cohort/KO.json"
pytestmark = pytest.mark.skipif(not BUNDLE.exists(), reason="reference bundle not collected")


def bundle() -> DataBundle:
    return DataBundle.model_validate_json(BUNDLE.read_text(encoding="utf-8"))


def pieces():
    data = bundle()
    assert data.raw is not None
    assembled = assemble(data, marginal_tax=0.25)
    wacc = compute_wacc(assembled.wacc_inputs)
    normalized = normalize(data.raw, Policy(marginal_tax_rate=0.25))[0]
    forecast = ForecastAgent(None).run(data, normalized, wacc, assembled)
    assert forecast.scenarios is not None
    return data, wacc, forecast, value_scenarios(data, forecast.scenarios, wacc, 0.25)


def test_the_valuation_record_weights_the_scenarios_and_grids_the_base():
    _, _, forecast, valuation = pieces()
    assert {r.scenario for r in valuation.results} == {"bear", "base", "bull"}
    assert valuation.low <= valuation.weighted <= valuation.high
    assert len(valuation.grid) == 5 and len(valuation.grid[0]) == 5
    # The grid's centre reruns the engine (one rate path, dilution iterated): within 0.1%.
    base = valuation.scenario("base")
    assert base is not None and valuation.grid[2][2] == pytest.approx(
        base.value_per_share, rel=1e-3
    )
    assert valuation.price is not None  # context only


@pytest.mark.parametrize(
    ("intent", "question", "expected"),
    [
        ("I6", "what is the fair value per share?", "Value per share (probability-weighted)"),
        ("I5", "what are the bull and bear cases?", "Bull (25%)"),
        ("I7", "what if WACC changes?", "WACC \\ terminal growth"),
        ("I4", "why this margin?", "Margin (margin, year 5"),
    ],
)
def test_valuation_answers_are_cited(intent, question, expected):
    data, wacc, forecast, valuation = pieces()
    request = data.request.model_copy(update={"intent": intent, "question": question})
    state = {
        "request": request,
        "data": data,
        "wacc": wacc,
        "forecast": forecast.scenarios,
        "valuation": valuation,
        "forecast_notes": tuple(forecast.notes),
    }
    reply = render(state)
    assert expected in reply.body
    if intent != "I7":  # the grid is a table of engine reruns, not of sourced figures
        assert reply.figures and all(f.source >= 1 for f in reply.figures)


def test_quick_value_runs_through_the_graph(tmp_path: Path):
    data = bundle()

    def forecast(b, normalized, wacc):
        assembled = assemble(b, marginal_tax=0.25)
        result = ForecastAgent(None).run(b, normalized, wacc, assembled)
        return result.scenarios, tuple(result.notes)

    services = Services(
        router=router(),
        collect=lambda request, industry: (
            data.model_copy(
                update={
                    "request": data.request.model_copy(
                        update={
                            "plan": request.plan,
                            "intent": request.intent,
                            "mode": request.mode,
                        }
                    )
                }
            ),
            None,
        ),
        wacc=lambda b: (compute_wacc(assemble(b, marginal_tax=0.25).wacc_inputs), None),
        forecast=forecast,
        now=lambda: datetime(2026, 10, 15, 12, tzinfo=UTC),
    )
    graph = build_graph(services, open_checkpointer(tmp_path / "c.sqlite"))
    start(graph, "v1", f"{data.request.ticker}, {data.request.valuation_date}")
    shown = resume(graph, "v1", {"action": "confirm", "output": "quick"})
    assert shown["__interrupt__"][0].value["kind"] == "assumptions"  # Gate 2 before the DCF
    final = resume(graph, "v1", {"action": "approve"})
    assert final["status"] == "done" and final["valuation"].weighted > 0
    assert [a.gate for a in final["approvals"]][-1] == 2
    assert final["done"][-5:] == ("forecast", "value", "comps", "critic", "answer")
    assert final["critique"].passed and final["critique"].revision_count == 0
    assert "Value per share (probability-weighted)" in final["answer"]


def test_every_number_in_the_valuation_is_sourced_and_justified():
    from dcf_agent.engine.audit import audit_records, audit_state

    data, wacc, forecast, valuation = pieces()
    assert data.raw is not None
    normalized = normalize(data.raw, Policy(marginal_tax_rate=0.25))[0]
    result = audit_state(
        {"data": data, "normalized": normalized, "wacc": wacc, "forecast": forecast.scenarios}
    )
    assert result.records > 2000 and result.clean, result.problems[:5]
    assert result.by_source["llm_estimate"] == 0
    base = next(s for s in forecast.scenarios.scenarios if s.scenario == "base")
    # Each forecast driver points to the inputs it is built from, and says how.
    assert "https://" in base.terminal_growth.prov.ref and "policy" in base.terminal_growth.prov.ref
    assert "min(risk-free" in (base.terminal_growth.prov.note or "")
    # A generic reference is caught.
    from dcf_agent.contracts.provenance import Provenance

    vague = Provenance(
        source="derived",
        ref="history",
        as_of=data.request.valuation_date,
        confidence="medium",
        note="x",
    )
    assert not audit_records([vague]).clean
