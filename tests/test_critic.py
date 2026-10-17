"""Critic: seeded flaws in a real valuation (KO), each caught by its own check."""

from collections.abc import Callable
from typing import Any

import pytest
from test_valuation_graph import BUNDLE, pieces

from dcf_agent.agents.critic import review
from dcf_agent.contracts.modeling import CompsResult, ForecastAssumptions
from dcf_agent.contracts.provenance import Sourced
from dcf_agent.engine.normalize import Policy, normalize

pytestmark = pytest.mark.skipif(not BUNDLE.exists(), reason="reference bundle not collected")

State = dict[str, Any]


def clean() -> State:
    data, wacc, forecast, valuation = pieces()
    assert data.raw is not None and forecast.scenarios is not None
    debt = abs(valuation.results[0].bridge["debt"].value)
    cash = valuation.results[0].bridge["cash"].value
    return {
        "data": data,
        "wacc": wacc,
        "forecast": forecast.scenarios,
        "valuation": valuation,
        "normalized": normalize(data.raw, Policy(marginal_tax_rate=0.25))[0],
        "forecast_notes": forecast.notes,
        "comps_target": {"debt": debt, "cash_only": cash},
        "parity": "8/8 within 1 cent",
    }


def at(item: Sourced[float], value: float) -> Sourced[float]:
    return item.model_copy(update={"value": value})


def base_edit(state: State, change: Callable[[ForecastAssumptions], dict[str, Any]]) -> State:
    scenarios = state["forecast"]
    edited = tuple(
        s.model_copy(update=change(s)) if s.scenario == "base" else s for s in scenarios.scenarios
    )
    return {**state, "forecast": scenarios.model_copy(update={"scenarios": edited})}


def growth_above_rf(s: State) -> State:
    rf = s["wacc"].rf.value
    return base_edit(s, lambda b: {"terminal_growth": at(b.terminal_growth, rf + 0.005)})


def growth_above_cap(s: State) -> State:
    return base_edit(s, lambda b: {"terminal_growth": at(b.terminal_growth, 0.045)})


def roic_below_wacc(s: State) -> State:
    low = s["wacc"].terminal_wacc.value - 0.02
    return base_edit(s, lambda b: {"terminal_roic": at(b.terminal_roic, low)})


def roic_without_moat(s: State) -> State:
    high = s["wacc"].terminal_wacc.value + 0.12
    return base_edit(
        s,
        lambda b: {
            "terminal_roic": at(b.terminal_roic, high),
            "rationale": {k: v for k, v in b.rationale.items() if k != "moat"},
        },
    )


def margin_jump(s: State) -> State:
    def change(b: ForecastAssumptions) -> dict[str, Any]:
        margins = list(b.operating_margin)
        margins[2] = at(margins[2], margins[1].value + 0.10)
        return {"operating_margin": tuple(margins)}

    return base_edit(s, change)


def late_growth(s: State) -> State:
    return base_edit(
        s,
        lambda b: {
            "revenue_growth": tuple(
                at(g, 0.30) if k >= 5 else g for k, g in enumerate(b.revenue_growth)
            )
        },
    )


def tax_not_converging(s: State) -> State:
    return base_edit(s, lambda b: {"tax_rate": (*b.tax_rate[:-1], at(b.tax_rate[-1], 0.12))})


def broken_reinvestment(s: State) -> State:
    return base_edit(
        s,
        lambda b: {"sales_to_capital": tuple(at(x, x.value * 25) for x in b.sales_to_capital)},
    )


def margin_far_above(s: State) -> State:
    return base_edit(
        s, lambda b: {"operating_margin": tuple(at(m, 0.85) for m in b.operating_margin)}
    )


def tv_share(s: State) -> State:
    valuation = s["valuation"]
    results = tuple(r.model_copy(update={"tv_share_of_ev": 0.88}) for r in valuation.results)
    return {**s, "valuation": valuation.model_copy(update={"results": results})}


def comps_divergence(s: State) -> State:
    comps = CompsResult.model_construct(
        as_of=s["valuation"].as_of,
        peers=(),
        summaries=(),
        implied_value_range=(70.0, 90.0),
        divergence_vs_dcf=-0.55,
        calculation_ref="dcf_agent.agents.comps",
        warnings=("seeded",),
    )
    return {**s, "comps": comps}


def price_unexplained(s: State) -> State:
    valuation = s["valuation"]
    return {
        **s,
        "valuation": valuation.model_copy(update={"price": valuation.weighted * 3}),
        "forecast_notes": (),
    }


def wrong_debt(s: State) -> State:
    return {**s, "comps_target": {**s["comps_target"], "debt": s["comps_target"]["debt"] * 1.4}}


def wrong_cash(s: State) -> State:
    return {
        **s,
        "comps_target": {**s["comps_target"], "cash_only": s["comps_target"]["cash_only"] / 2},
    }


def parity_break(s: State) -> State:
    return {**s, "parity": "6/8: base 45.12 vs 47.80"}


def wacc_below_rf(s: State) -> State:
    wacc = s["wacc"]
    return {**s, "wacc": wacc.model_copy(update={"wacc": at(wacc.wacc, wacc.rf.value - 0.01)})}


def unsourced(s: State) -> State:
    def change(b: ForecastAssumptions) -> dict[str, Any]:
        first = b.operating_margin[0]
        guess = first.model_copy(
            update={"prov": first.prov.model_copy(update={"source": "llm_estimate"})}
        )
        return {"operating_margin": (guess, *b.operating_margin[1:])}

    return base_edit(s, change)


FLAWS: list[tuple[Callable[[State], State], str]] = [
    (growth_above_rf, "terminal_growth"),
    (growth_above_cap, "terminal_growth"),
    (roic_below_wacc, "terminal_roic"),
    (roic_without_moat, "terminal_roic"),
    (margin_jump, "margin_step"),
    (late_growth, "late_growth"),
    (tax_not_converging, "tax"),
    (broken_reinvestment, "reinvestment"),
    (margin_far_above, "margin_level"),
    (tv_share, "tv_share"),
    (comps_divergence, "comps"),
    (price_unexplained, "price"),
    (wrong_debt, "bridge"),
    (wrong_cash, "bridge"),
    (parity_break, "parity"),
    (wacc_below_rf, "wacc"),
    (unsourced, "provenance"),
]


def checks(state: State) -> set[str]:
    return {i.issue_id.split(":")[0] for i in review(state).issues}


def test_the_clean_valuation_passes():
    report = review(clean())
    assert report.passed and not [i for i in report.issues if i.severity == "high"]


@pytest.mark.parametrize(("seed", "check"), FLAWS, ids=[f[0].__name__ for f in FLAWS])
def test_a_seeded_flaw_is_caught_by_its_check(seed, check):
    state = seed(clean())
    report = review(state)
    caught = {i.issue_id.split(":")[0]: i for i in report.issues}
    assert check in caught, [i.description for i in report.issues]
    issue = caught[check]
    assert issue.evidence_refs and issue.suggested_fix and issue.route_back_to


def test_recall_on_the_seeded_flaws_is_at_least_90_percent():
    base = clean()
    found = sum(check in checks(seed(base)) for seed, check in FLAWS)
    assert found / len(FLAWS) >= 0.9


def test_issues_are_routed_to_their_owner():
    base = clean()
    owners = {
        check: next(
            i.route_back_to for i in review(seed(base)).issues if i.issue_id.startswith(check)
        )
        for seed, check in FLAWS
    }
    assert owners["terminal_growth"] == "forecast"
    assert owners["wacc"] == "wacc"
    assert owners["bridge"] == "data"
    assert owners["parity"] == "reporter"


def test_high_severity_fails_the_review_and_counts_loops():
    report = review(growth_above_rf(clean()), revision=2)
    assert not report.passed and report.revision_count == 2 and report.score < 5


# Loops in the graph: a fixable issue is fixed by its owner; the loop stops at the cap.
def loop_graph(tmp_path, flaw=None, rubric=None, calls=None):
    from datetime import UTC, datetime

    from test_router import router
    from test_valuation_graph import bundle

    from dcf_agent.agents.forecast import ForecastAgent
    from dcf_agent.engine.assemble import assemble
    from dcf_agent.engine.wacc import compute_wacc
    from dcf_agent.graph.workflow import Services, build_graph, open_checkpointer

    data = bundle()

    def forecast(b, normalized, wacc, note=None):
        if calls is not None:
            calls.append(note)
        scenarios = ForecastAgent(None).run(b, normalized, wacc, assemble(b, marginal_tax=0.25))
        result = scenarios.scenarios
        if flaw is not None and note is None:  # the first forecast carries the seeded flaw
            result = flaw({"forecast": result, "wacc": wacc})["forecast"]
        return result, tuple(scenarios.notes)

    services = Services(
        router=router(),
        collect=lambda request, industry: (data.model_copy(update={"request": request}), None),
        wacc=lambda b: (compute_wacc(assemble(b, marginal_tax=0.25).wacc_inputs), None),
        forecast=forecast,
        rubric=rubric,
        now=lambda: datetime(2026, 10, 15, 12, tzinfo=UTC),
    )
    saver = open_checkpointer(tmp_path / "c.sqlite")
    return build_graph(services, saver), saver, data


def run_to_end(graph, data):
    from dcf_agent.graph.workflow import resume, start

    start(graph, "c1", f"{data.request.ticker}, {data.request.valuation_date}")
    resume(graph, "c1", {"action": "confirm", "output": "quick"})
    return resume(graph, "c1", {"action": "approve"})


def test_a_seeded_high_issue_is_fixed_by_its_owner_in_one_loop(tmp_path):
    graph, saver, data = loop_graph(tmp_path, flaw=growth_above_cap)
    final = run_to_end(graph, data)
    report = final["critique"]
    assert final["status"] == "done" and report.passed and report.revision_count == 1
    assert final["critic_changes"] == ("base: terminal growth 4.50% -> 4.00%",)
    base = next(s for s in final["forecast"].scenarios if s.scenario == "base")
    assert base.terminal_growth.value == pytest.approx(0.04)
    assert "Critic fix" in (base.terminal_growth.prov.note or "")
    assert "dcf_agent.agents.critic" in base.terminal_growth.prov.ref
    saver.conn.close()


def test_the_loop_stops_at_the_cap_and_leaves_the_issue_for_gate3(tmp_path):
    from dcf_agent.contracts.workflow import CritiqueIssue, CritiqueReport

    def stubborn(state, report):  # a high forecast issue no re-think removes
        issue = CritiqueIssue(
            issue_id="rubric:99",
            severity="high",
            description="story contradicts the margins",
            evidence_refs=("base.operating_margin",),
            suggested_fix="rewrite the story",
            route_back_to="forecast",
        )
        return CritiqueReport(
            issues=(*report.issues, issue),
            score=2,
            passed=False,
            revision_count=report.revision_count,
        )

    calls: list[str | None] = []
    graph, saver, data = loop_graph(tmp_path, rubric=stubborn, calls=calls)
    final = run_to_end(graph, data)
    assert final["status"] == "done" and final["critique"].revision_count == 2
    assert not final["critique"].passed
    assert len(calls) == 3 and all(c and c.startswith("Critic review") for c in calls[1:])
    critic_runs = [
        e for e in final["audit_log"] if e.component == "critic" and "Score" in (e.rationale or "")
    ]
    assert len(critic_runs) == 3  # first review and two loops, then the cap
    saver.conn.close()


def test_an_issue_its_owner_cannot_change_is_not_looped(tmp_path):
    from dcf_agent.contracts.workflow import CritiqueIssue, CritiqueReport

    def data_issue(state, report):
        issue = CritiqueIssue(
            issue_id="bridge:99",
            severity="high",
            description="debt differs from the filing",
            evidence_refs=("SEC company facts",),
            suggested_fix="recheck the debt",
            route_back_to="data",
        )
        return CritiqueReport(issues=(issue,), score=3, passed=False)

    graph, saver, data = loop_graph(tmp_path, rubric=data_issue)
    final = run_to_end(graph, data)
    assert final["critique"].revision_count == 0 and not final.get("critic_changes")
    assert any("left for review" in (e.rationale or "") for e in final["audit_log"])
    saver.conn.close()


def test_the_rubric_adds_cited_concerns_and_never_clears_an_issue():
    from dcf_agent.agents.critic import Concern, Rubric, evidence_list, risk_passages, with_rubric

    state = growth_above_rf(clean())
    report = review(state)
    risks = risk_passages(
        [
            ("accession A", "In addition to the other information, carefully consider ..."),
            ("accession B", "Changes in consumer preferences could reduce demand."),
        ]
    )
    assert risks == [("accession B", "Changes in consumer preferences could reduce demand.")]
    ids = evidence_list(state, risks)
    assert set(ids) == {"bear.story", "base.story", "bull.story", "risk1"}
    rubric = Rubric(
        narrative_consistency=4,
        evidence_quality=4,
        scenario_distinctness=3,
        unsupported_claims=5,
        missing_risks=2,
        concerns=[
            Concern(description="demand risk missing", evidence="risk1", severity="medium"),
            Concern(description="made up", evidence="page 7", severity="medium"),
        ],
    )
    out = with_rubric(report, rubric, ids)
    added = [i for i in out.issues if i.issue_id.startswith("rubric")]
    assert len(added) == 1 and added[0].evidence_refs == ("risk1: accession B",)
    assert added[0].route_back_to == "forecast" and added[0].severity == "medium"
    assert not out.passed and set(report.issues) <= set(out.issues)  # nothing cleared
    assert out.rubric_scores["missing_risks"] == 2 and out.score < 5


def test_more_debt_in_the_bridge_than_the_tagged_debt_is_expected():
    state = clean()
    state["comps_target"] = {**state["comps_target"], "debt": state["comps_target"]["debt"] * 0.8}
    assert "bridge" not in checks(state)  # current maturities, paper, finance leases on top
