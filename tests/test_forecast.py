"""Forecast Agent: anchors and worlds by code, levels and stories by the model."""

import json
from datetime import date
from pathlib import Path

from pytest import approx
from test_drivers import AS_OF, history, period, sourced
from test_insights import data_bundle
from test_llm import config as llm_config
from test_wacc import inputs

from dcf_agent.agents.forecast import (
    ForecastAgent,
    anchors,
    margin_target,
)
from dcf_agent.contracts.accounting import FilingCitation, FilingItem
from dcf_agent.contracts.inputs import RawFinancials
from dcf_agent.engine.assemble import Assembled
from dcf_agent.engine.dcf import Shares
from dcf_agent.engine.drivers import Benchmarks, check_coherence
from dcf_agent.engine.normalize import Policy, normalize
from dcf_agent.engine.wacc import compute_wacc
from dcf_agent.llm import LocalLLM

WACC = compute_wacc(inputs())


def industry(margin: float, turnover: float) -> Benchmarks:
    return Benchmarks("Example", sourced(margin, None), sourced(turnover, None))


def guidance(metric: str, value: float) -> FilingItem:
    return FilingItem(
        item_id=f"guidance:{metric}:1",
        description=f"{metric} guidance",
        citation=FilingCitation(
            accession="0000000001-26-000010", filed=date(2026, 7, 28), section="EX-99.1"
        ),
        confidence="medium",
        amount=sourced(value, None).model_copy(
            update={
                "prov": sourced(value, None).prov.model_copy(
                    update={"source": "sec_filing_text", "filed": date(2026, 7, 28)}
                )
            }
        ),
    )


def test_anchors_use_guidance_first_and_bound_sales_to_capital():
    stats = history()
    guided = anchors(stats, None, WACC, [guidance("revenue_growth", 0.05)])
    assert guided.growth == 0.05 and "guidance" in guided.notes["growth"]
    blended = anchors(stats, None, WACC, [])
    assert abs(blended.growth - 0.1) < 1e-3  # half latest growth, half CAGR (both 10%)
    own = stats.sales_to_capital.median or 0
    assert anchors(stats, industry(0.2, own * 2), WACC, []).sales_to_capital == own * 2  # floor
    assert anchors(stats, industry(0.2, own / 10), WACC, []).sales_to_capital == own / 10 * 2.5
    assert blended.terminal_growth == min(WACC.rf.value, 0.03)


def test_margin_targets_respect_the_industry_and_the_company_history():
    peer = industry(0.20, 1.5)
    assert margin_target("holds", 0.30, peer, 0.31) == 0.30
    assert margin_target("expands", 0.30, peer, 0.31) == approx(0.325)  # near its own best year
    assert margin_target("compresses", 0.30, peer, 0.31) < 0.30 - 0.02
    assert margin_target("expands", 0.10, peer, 0.12) == approx(0.15)  # closes half the gap
    # A loss maker converges toward the better of the industry and its guided margin.
    assert margin_target("holds", -0.3, peer, -0.3, guided=0.25) == 0.25


def assembled() -> Assembled:
    base = period(2025, 5_324e6, 1_150e6)
    return Assembled(
        as_of=AS_OF,
        base_period=base,
        base_revenue=5_324e6,
        wacc_inputs=inputs(),
        bridge={"cash": sourced(500e6, date(2025, 12, 31))},
        shares=Shares(basic=100e6),
    )


def normalized():
    raw = RawFinancials(
        cik="0000000001",
        as_of=AS_OF,
        annual=(
            period(2022, 4_000e6, 800e6),
            period(2023, 4_400e6, 900e6),
            period(2024, 4_840e6, 1_000e6),
            period(2025, 5_324e6, 1_150e6),
        ),
    )
    return normalize(raw, Policy(marginal_tax_rate=0.25))[0]


CHOICE = {
    "growth": "fades_normally",
    "growth_evidence": "10% a year for three years",
    "margin": "holds",
    "margin_evidence": "margin rising to 21.6%",
    "reinvestment": "history",
    "reinvestment_evidence": "sales to capital stable",
    "moat": "narrow",
    "moat_evidence": "ROIC above WACC",
}
BASE_STORY = (
    "Demand keeps growing as the company adds customers, and pricing holds because "
    "costs are stable, so margins stay where they are today."
)
STORIES = {
    "bear": "growth fades fast, margin compresses",
    "base": BASE_STORY,
    "bull": "Growth lasts.",
}


def test_the_agent_builds_ordered_valid_scenarios_from_the_chosen_levels(tmp_path: Path):
    replies = [json.dumps(CHOICE), json.dumps(STORIES)]
    with LocalLLM(llm_config(tmp_path), mock_responses=replies) as llm:
        forecast = ForecastAgent(llm).run(data_bundle(None), normalized(), WACC, assembled())
    assert forecast.chosen_by == "model" and forecast.model_calls == 2
    assert forecast.scenarios is not None
    by = {s.scenario: s for s in forecast.scenarios.scenarios}
    assert (forecast.picked["base"].growth, forecast.picked["base"].margin) == (
        "fades_normally",
        "holds",
    )
    assert by["base"].narrative == BASE_STORY and by["bull"].probability == 0.25
    # A story that only echoes the labels, or is too short, is replaced by the facts.
    assert by["bear"].narrative.startswith("Bear case: revenue growth starts at")
    assert by["bull"].narrative.startswith("Bull case:")
    assert all(check_coherence(s, WACC).valid for s in by.values())
    values = {s: w.value for s, w in forecast.picked.items()}
    assert values["bear"] < values["base"] < values["bull"]
    assert "10% a year" in by["base"].rationale["growth"]  # the model's evidence, cited
    assert all(d.prov.source == "derived" for d in by["base"].operating_margin)


def test_without_the_model_the_default_levels_apply():
    forecast = ForecastAgent(None).run(data_bundle(None), normalized(), WACC, assembled())
    assert forecast.chosen_by == "default" and forecast.model_calls == 0
    assert forecast.scenarios is not None and forecast.choice.growth == "fades_normally"


def test_a_model_level_the_statistics_do_not_support_is_rejected_and_flagged():
    from dcf_agent.agents.forecast import AxisChoice, reconcile, rule_choice

    stats = history()  # steady 10% growth, rising margins
    a = anchors(stats, None, WACC, [])
    rule = rule_choice(stats, WACC, a, None)
    assert rule.margin == "expands" and rule.growth == "fades_normally"
    read = AxisChoice.model_validate({**CHOICE, "margin": "compresses", "growth": "fades_fast"})
    choice, rejected = reconcile(read, rule, stats, WACC, a, None)
    assert choice.margin == "expands" and choice.growth == "fades_normally"
    assert len(rejected) == 2 and all("not supported" in r for r in rejected)
    kept, none = reconcile(AxisChoice.model_validate(CHOICE), rule, stats, WACC, a, None)
    assert kept.margin == "holds" and not none  # holding is always plausible
