"""Batch runner: outcomes and failure classes, crash isolation, time limit, resume."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from test_critic import growth_above_rf, unsourced
from test_router import router
from test_valuation_graph import BUNDLE, bundle

from dcf_agent.agents.forecast import ForecastAgent
from dcf_agent.batch import run_batch, run_company, summarize
from dcf_agent.contracts.workflow import Budget
from dcf_agent.engine.assemble import assemble
from dcf_agent.engine.wacc import compute_wacc
from dcf_agent.graph.workflow import Services, build_graph, open_checkpointer

pytestmark = pytest.mark.skipif(not BUNDLE.exists(), reason="reference bundle not collected")
AS_OF = date(2026, 10, 11)


def graph_for(tmp_path: Path, *, flaw=None, sic="3571", collect_error=None, answer_error=None):
    data = bundle()

    def forecast(b, normalized, wacc, note=None):
        result = ForecastAgent(None).run(b, normalized, wacc, assemble(b, marginal_tax=0.25))
        scenarios = result.scenarios
        if flaw is not None:
            scenarios = flaw({"forecast": scenarios, "wacc": wacc})["forecast"]
        return scenarios, tuple(result.notes)

    def collect(request, industry):
        if collect_error is not None and request.ticker in collect_error:
            raise RuntimeError("no 10-K statements found")
        return data.model_copy(update={"request": request}), None

    def answer(state):
        if answer_error is not None:
            raise ValueError(answer_error)
        from dcf_agent.agents.answer import render

        return render(state)

    services = Services(
        router=router(sic),
        collect=collect,
        wacc=lambda b: (compute_wacc(assemble(b, marginal_tax=0.25).wacc_inputs), None),
        forecast=forecast,
        answer=answer,
        models=tmp_path / "models",
        reports=tmp_path / "reports",
        now=lambda: datetime(2026, 10, 15, 12, tzinfo=UTC),
    )
    saver = open_checkpointer(tmp_path / "c.sqlite")
    return build_graph(services, saver), saver


def test_a_clean_company_succeeds_unattended(tmp_path: Path):
    graph, saver = graph_for(tmp_path)
    result = run_company(graph, "KO", AS_OF, parity=False)
    assert result.outcome == "success" and result.failure is None, result.reason
    assert result.value is not None and result.audit_clean and result.report
    assert result.steps[-2:] == ["report", "answer"] and result.step_seconds
    assert (Path(result.report) / "memo.md").exists()
    saver.conn.close()


def test_each_failure_is_classified_with_advice(tmp_path: Path):
    graph, saver = graph_for(tmp_path / "a", flaw=growth_above_rf)
    engine = run_company(graph, "KO", AS_OF, parity=False)
    assert (engine.outcome, engine.failure) == ("failed", "engine")
    assert "Not valued" in engine.reason and engine.advice
    saver.conn.close()
    graph, saver = graph_for(tmp_path / "b", collect_error={"KO"})
    data = run_company(graph, "KO", AS_OF, parity=False)
    assert (data.outcome, data.failure) == ("failed", "data")
    assert "no 10-K statements" in data.reason
    saver.conn.close()
    graph, saver = graph_for(tmp_path / "c", sic="6022")  # a bank: outside v1
    scope = run_company(graph, "KO", AS_OF, parity=False)
    assert (scope.outcome, scope.failure) == ("failed", "scope")
    saver.conn.close()
    graph, saver = graph_for(tmp_path / "d")
    unknown = run_company(graph, "QQQQZZ", AS_OF, parity=False)
    assert (unknown.outcome, unknown.failure) == ("failed", "resolution")
    saver.conn.close()


def test_the_time_limit_stops_a_company(tmp_path: Path):
    graph, saver = graph_for(tmp_path)
    tight = Budget(max_steps=40, max_tokens=500_000, max_wall_seconds=0.001)
    result = run_company(graph, "KO", AS_OF, budget=tight, parity=False)
    assert (result.outcome, result.failure) == ("failed", "time_limit")
    saver.conn.close()


def test_an_export_with_a_failed_hard_check_is_a_wrong_number(tmp_path: Path):
    graph, saver = graph_for(tmp_path, flaw=unsourced)  # a model-estimated value
    result = run_company(graph, "KO", AS_OF, parity=False)
    assert (result.outcome, result.failure) == ("wrong_number", "checks")
    assert not result.audit_clean and "audit not clean" in result.reason
    saver.conn.close()


def test_a_crash_is_recorded_the_batch_continues_and_resumes(tmp_path: Path):
    out = tmp_path / "results.jsonl"
    graph, saver = graph_for(tmp_path, answer_error="boom")
    first = run_batch(graph, ["KO", "AAPL"], AS_OF, out, parity=False)
    assert [r.failure for r in first] == ["crash", "crash"]
    assert "ValueError: boom" in first[0].reason
    again = run_batch(graph, ["KO", "AAPL"], AS_OF, out, parity=False)
    assert again == []  # both recorded: nothing rerun
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert [r["ticker"] for r in rows] == ["KO", "AAPL"]
    summary = summarize(out)
    assert summary["companies"] == 2 and summary["failures"] == {"crash": ["KO", "AAPL"]}
    saver.conn.close()


def test_a_session_trace_renders_steps_approvals_and_timed_model_calls(tmp_path: Path):
    from dcf_agent.report.trace import model_calls, sessions, write_trace

    graph, saver = graph_for(tmp_path)
    run_company(graph, "KO", AS_OF, parity=False, session="traced")
    saver.conn.close()
    assert sessions(tmp_path / "c.sqlite") == ["traced"]
    traces = tmp_path / "llm.jsonl"
    traces.write_text(
        '{"at": "2026-10-15T12:00:00+00:00", "role": "forecast", "attempt": 2, '
        '"status": "valid", "duration_seconds": 30.0, "prompt_tokens": 900, '
        '"output_tokens": 300}\n'
        '{"role": "wacc", "duration_seconds": 5.0}\n'  # an old trace without a time
        "not json\n",
        encoding="utf-8",
    )
    page = write_trace(tmp_path / "c.sqlite", "traced", traces, tmp_path / "t.html")
    text = page.read_text(encoding="utf-8")
    for heading in ("Time by component", "Model calls by role", "Approvals", "Timeline"):
        assert heading in text
    assert "<td>forecast</td><td>1</td><td>30.0s</td><td>1200</td><td>1</td>" in text
    assert "<script" not in text
    start = datetime(2026, 10, 15, 11, tzinfo=UTC)
    assert len(model_calls(traces, start, datetime(2026, 10, 15, 13, tzinfo=UTC))) == 1
