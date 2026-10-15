"""WACC Agent: the model may only choose a method, and only with evidence code can verify."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_llm import config as llm_config
from test_wacc import inputs

from dcf_agent.agents import wacc as agent_module
from dcf_agent.agents.wacc import WaccAgent, assessment
from dcf_agent.engine.wacc import Regression, WaccError, compute_wacc, regression_beta
from dcf_agent.llm import LocalLLM

# bottom-up beta for inputs(): 0.8 x (1 + 0.75 x 0.25) = 0.95
BOTTOM_UP = 0.8 * (1 + 0.75 * 0.25)
NOISY = Regression(beta=1.4, r_squared=0.10, standard_error=0.30, observations=104)
IMPRECISE = Regression(beta=1.4, r_squared=0.40, standard_error=0.20, observations=104)
RELIABLE = Regression(
    beta=1.2, r_squared=0.50, standard_error=0.10, observations=60, frequency="monthly"
)


def agent(monkeypatch, weekly, monthly, llm=None) -> WaccAgent:
    monkeypatch.setattr(
        agent_module, "assemble", lambda bundle, **_: SimpleNamespace(wacc_inputs=inputs())
    )
    a = WaccAgent(Mock(), llm)
    monkeypatch.setattr(a, "regressions", lambda bundle: (weekly, monthly, []))
    return a


def run(monkeypatch, tmp_path: Path, weekly, monthly, *replies: dict):
    responses = [json.dumps(r) for r in replies]
    with LocalLLM(llm_config(tmp_path), mock_responses=responses) as llm:
        return agent(monkeypatch, weekly, monthly, llm).run(Mock())


def test_without_a_model_the_rule_decides_and_matches_the_engine(monkeypatch):
    result, decision = agent(monkeypatch, NOISY, None).run(Mock())
    assert decision.source == "rule" and decision.method == "bottom_up" and not decision.flagged
    assert result.wacc.value == compute_wacc(replace(inputs(), regression=NOISY)).wacc.value


def test_model_agreeing_with_the_rule_changes_nothing(monkeypatch, tmp_path):
    reply = {"method": "bottom_up", "reason": "Regressions are noisy."}
    result, decision = run(monkeypatch, tmp_path, NOISY, None, reply)
    assert decision.source == "model" and not decision.flagged and decision.rejected is None
    assert result.wacc.value == compute_wacc(replace(inputs(), regression=NOISY)).wacc.value
    assert any("chosen by the WACC Agent: Regressions are noisy." in n for n in result.method_notes)


def test_supported_regression_uses_the_reliable_window_and_is_flagged(monkeypatch, tmp_path):
    reply = {"method": "regression", "reason": "The five-year fit is reliable."}
    result, decision = run(monkeypatch, tmp_path, NOISY, RELIABLE, reply)
    assert decision.method == "regression" and decision.flagged
    assert decision.rule_method == "bottom_up"
    assert result.regression_beta.value == RELIABLE.beta
    assert result.cost_equity.value == pytest.approx(0.04 + RELIABLE.beta * 0.05)
    assert "Review: the default rule would use bottom_up" in result.method_notes
    assert result.method_notes[0].startswith("Bottom-up beta used")  # the rule's own verdict


def test_supported_average_is_the_midpoint(monkeypatch, tmp_path):
    reply = {"method": "average", "reason": "Explains the stock but imprecise."}
    result, decision = run(monkeypatch, tmp_path, IMPRECISE, None, reply)
    assert decision.method == "average" and decision.flagged
    beta = (BOTTOM_UP + IMPRECISE.beta) / 2
    assert result.cost_equity.value == pytest.approx(0.04 + beta * 0.05)


@pytest.mark.parametrize(
    ("method", "why"),
    [("regression", "meets the reliability rule"), ("average", "failing only the standard")],
)
def test_unsupported_departure_is_rejected_and_the_rule_applies(monkeypatch, tmp_path, method, why):
    reply = {"method": method, "reason": "Midpoint looks sensible."}
    result, decision = run(monkeypatch, tmp_path, NOISY, None, reply)
    assert decision.source == "rule" and decision.method == "bottom_up" and not decision.flagged
    assert why in decision.rejected
    assert result.wacc.value == compute_wacc(replace(inputs(), regression=NOISY)).wacc.value
    assert result.method_notes[-1].startswith("Rejected: model chose " + method)


def test_model_failure_falls_back_to_the_rule(monkeypatch, tmp_path):
    with LocalLLM(llm_config(tmp_path), mock_responses=["not json", "still not json"]) as llm:
        result, decision = agent(monkeypatch, IMPRECISE, None, llm).run(Mock())
    assert decision.source == "rule" and any("Model unavailable" in e for e in decision.evidence)


def test_without_any_regression_the_model_is_not_asked(monkeypatch, tmp_path):
    with LocalLLM(llm_config(tmp_path)) as llm:  # no responses: any call would fail
        result, decision = agent(monkeypatch, None, None, llm).run(Mock())
    assert decision.source == "rule" and not any("Model" in e for e in decision.evidence)


def test_prompt_states_each_threshold_verdict(monkeypatch):
    text = agent(monkeypatch, IMPRECISE, RELIABLE).prompt(
        inputs(), BOTTOM_UP, IMPRECISE, RELIABLE, "bottom_up"
    )
    assert "The default rule chooses: bottom_up" in text
    assert "R^2 0.40 (meets >= 0.3); standard error 0.20 (fails <= 0.15)" in text
    assert "reliable under the rule." in assessment("x", RELIABLE)


def test_engine_override_needs_a_regression():
    with pytest.raises(WaccError, match="needs a regression"):
        compute_wacc(replace(inputs(), beta_method="average"))


def test_monthly_regression_uses_month_end_returns():
    from datetime import date, timedelta

    days = [date(2021, 1, 1) + timedelta(days=d) for d in range(0, 5 * 365, 7)]
    market = [(d, 100 * (1.01 ** (i % 7)) * (1 + 0.002 * i)) for i, d in enumerate(days)]
    stock = [(d, p**1.5) for d, p in market]  # log-return beta exactly 1.5
    fit = regression_beta(stock, market, frequency="monthly")
    assert fit.frequency == "monthly" and 50 <= fit.observations <= 60
    assert fit.beta == pytest.approx(1.5, rel=0.1) and fit.r_squared > 0.9
    with pytest.raises(WaccError, match="months"):
        regression_beta(stock[:100], market[:100], frequency="monthly")
