"""Critic: an adversarial review of the valuation, deterministic checks first.

Every check reads the session's state and cites its evidence. Issues carry a
severity, a suggested fix and the step that owns the fix (forecast, WACC,
normalization, data, comps); high-severity issues send the state back to that step
(at most two loops), the rest are warnings at Gate 3. The model's rubric (step 4)
adds to these checks; it never clears one.

Checks (docs/02-agents.md section 8, plus the WACC and provenance):
- terminal value share of EV, terminal growth (risk-free rate, 4%), terminal ROIC
  (below WACC; more than 5 points above without a moat rationale);
- reinvestment: the return on new capital implied by margin, tax and sales-to-capital;
- margins: a step of more than 5 points in a year, or far above the industry and the
  company's own history; growth above 25% beyond year 5; tax converging to marginal;
- DCF against comps (more than 40%) and against the price (more than 50%, unexplained);
- the bridge's debt and cash against the filings' own figures (more than 2%);
- WACC below the risk-free rate or above 20%, a cost of equity below the risk-free
  rate, a beta outside 0.3-3; workbook parity; the provenance audit.
"""

import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.modeling import (
    CompsResult,
    ForecastAssumptions,
    ForecastScenarios,
    ValuationResult,
    WACCResult,
)
from dcf_agent.contracts.provenance import Sourced
from dcf_agent.contracts.workflow import CritiqueIssue, CritiqueReport, Node
from dcf_agent.engine.audit import audit_state
from dcf_agent.engine.drivers import GDP_CAP, DriverStats, driver_stats, industry_benchmarks
from dcf_agent.engine.valuation import Valuation
from dcf_agent.llm import LLMError, LocalLLM

Severity = Literal["low", "medium", "high"]
TV_SHARE = 0.75
MOAT_SPREAD = 0.05
MARGIN_STEP = 0.05
LATE_GROWTH = 0.25
COMPS_GAP = 0.40
PRICE_GAP = 0.50
BRIDGE_GAP = 0.02
NEW_CAPITAL_RETURN = 1.0  # return on new capital above 100%: growth almost free of capital
ROIC_HEADROOM = 1.25  # ... and above the company's own best ROIC by a quarter
BETA_RANGE = (0.3, 3.0)
WACC_CEILING = 0.20


class Issues:
    """Collects issues with unique IDs."""

    def __init__(self) -> None:
        self.items: list[CritiqueIssue] = []

    def add(
        self,
        check: str,
        severity: Severity,
        description: str,
        evidence: Sequence[str],
        fix: str,
        owner: Node,
    ) -> None:
        self.items.append(
            CritiqueIssue(
                issue_id=f"{check}:{len(self.items) + 1}",
                severity=severity,
                description=description,
                evidence_refs=tuple(e for e in evidence if e) or ("dcf_agent.agents.critic",),
                suggested_fix=fix,
                route_back_to=owner,
            )
        )


def scenario_checks(
    item: ForecastAssumptions,
    wacc: WACCResult,
    issues: Issues,
    history: DriverStats | None,
    industry_margin: float | None,
) -> None:
    name = item.scenario
    g, roic = item.terminal_growth, item.terminal_roic
    rf, terminal_wacc = wacc.rf.value, wacc.terminal_wacc.value
    if g.value > rf + 1e-12 or g.value > GDP_CAP + 1e-12:
        # Above the cap by the user's own override (with a reason): a warning for Gate 3,
        # never sent back; above the risk-free rate the engine refuses it anyway.
        chosen = g.prov.source == "user_override" and g.value <= rf
        issues.add(
            "terminal_growth",
            "medium" if chosen else "high",
            f"{name}: terminal growth {g.value:.2%} above the risk-free rate {rf:.2%} or the "
            f"{GDP_CAP:.0%} long-run cap" + (" (your override at Gate 2)" if chosen else ""),
            (g.prov.ref, wacc.rf.prov.ref),
            f"set {name} terminal growth at most {min(rf, GDP_CAP):.2%}",
            "forecast",
        )
    if roic.value < terminal_wacc:
        issues.add(
            "terminal_roic",
            "medium",
            f"{name}: terminal ROIC {roic.value:.2%} below the terminal WACC "
            f"{terminal_wacc:.2%}: growth destroys value in perpetuity",
            (roic.prov.ref, wacc.terminal_wacc.prov.ref),
            f"raise {name} terminal ROIC to at least the terminal WACC, or lower terminal growth",
            "forecast",
        )
    elif roic.value > terminal_wacc + MOAT_SPREAD and "moat" not in item.rationale:
        issues.add(
            "terminal_roic",
            "medium",
            f"{name}: terminal ROIC {roic.value:.2%} more than {MOAT_SPREAD:.0%} above WACC "
            "without a moat rationale",
            (roic.prov.ref,),
            "justify the moat or lower terminal ROIC toward WACC",
            "forecast",
        )
    margins = [m.value for m in item.operating_margin]
    for year, (a, b) in enumerate(zip(margins, margins[1:], strict=False), start=2):
        if abs(b - a) > MARGIN_STEP:
            issues.add(
                "margin_step",
                "medium",
                f"{name}: operating margin moves {b - a:+.1%} in year {year} ({a:.1%} to {b:.1%})",
                (item.operating_margin[year - 1].prov.ref,),
                "spread the margin change over several years",
                "forecast",
            )
            break
    history_high = max(history.margin.numbers, default=None) if history else None
    if industry_margin is not None and history_high is not None:
        peak = max(margins)
        if peak > max(2 * industry_margin, industry_margin + 0.15) and peak > history_high + 0.02:
            issues.add(
                "margin_level",
                "medium",
                f"{name}: margin {peak:.1%} far above the industry ({industry_margin:.1%}) and "
                f"the company's best year ({history_high:.1%})",
                (item.operating_margin[0].prov.ref,),
                "bring the margin back toward the company's own history",
                "forecast",
            )
    late = [k + 1 for k, v in enumerate(item.revenue_growth) if k >= 5 and v.value > LATE_GROWTH]
    if late:
        issues.add(
            "late_growth",
            "medium",
            f"{name}: revenue growth above {LATE_GROWTH:.0%} in years {late[0]}-{late[-1]}",
            (item.revenue_growth[late[0] - 1].prov.ref,),
            "fade growth faster after year 5",
            "forecast",
        )
    tax = item.tax_rate
    if abs(tax[-1].value - item.marginal_tax_rate.value) > 1e-9:
        issues.add(
            "tax",
            "high",
            f"{name}: tax {tax[-1].value:.1%} in the last year, not the marginal "
            f"{item.marginal_tax_rate.value:.1%}",
            (tax[-1].prov.ref, item.marginal_tax_rate.prov.ref),
            "fade the tax rate to the marginal rate by the last year",
            "forecast",
        )
    # The return on new capital implied by the drivers: margin x (1 - tax) x sales-to-capital.
    returns = [
        m.value * (1 - t.value) * s.value
        for m, t, s in zip(item.operating_margin, item.tax_rate, item.sales_to_capital, strict=True)
    ]
    best = max(history.roic.numbers, default=0.0) if history else 0.0
    if max(returns) > max(NEW_CAPITAL_RETURN, ROIC_HEADROOM * best):
        issues.add(
            "reinvestment",
            "medium",
            f"{name}: new capital would return {max(returns):.0%} a year (margin x (1 - tax) x "
            f"sales-to-capital), above the company's best ROIC ({best:.0%}): growth almost "
            "without investment",
            (item.sales_to_capital[0].prov.ref,),
            "lower sales-to-capital toward the company's or the industry's",
            "forecast",
        )


def bridge_checks(
    result: ValuationResult, target: dict[str, float], bundle: DataBundle, issues: Issues
) -> None:
    """The bridge's cash and debt against the filing's own tags, read independently.

    Like for like: cash against the filing's cash and equivalents (both ways); debt only
    when the bridge holds less than the filing's long-term debt and borrowings (the
    bridge adds current maturities, commercial paper and finance leases, so more is
    expected; less means debt may be missing). Captive finance debt is set aside, and a
    read of another period (an older cached filing) is not compared.
    """
    period = bundle.raw.ttm if bundle.raw is not None else None
    statuses = period.statuses if period is not None else {}
    captive = statuses.get("captive_finance_debt")
    checks = (
        ("cash", "cash", target.get("cash_only"), False),
        ("debt", "debt", target.get("debt"), captive is not None and captive.status == "reported"),
    )
    for name, concept, theirs, skip in checks:
        item = result.bridge.get(concept)
        if skip or item is None or not theirs:
            continue
        end = target.get(f"{'cash_only' if name == 'cash' else name}_end")
        same = item.prov.period_end is None or end is None
        if not same and item.prov.period_end is not None and end is not None:
            same = item.prov.period_end.toordinal() == int(end)
        if not same:
            continue
        ours = abs(item.value)
        gap = (ours - theirs) / abs(theirs)
        if abs(gap) <= BRIDGE_GAP or (name == "debt" and gap > 0):
            continue
        issues.add(
            "bridge",
            "medium" if abs(gap) < 0.25 else "high",
            f"{name} in the bridge {ours:,.0f} differs from the filing's {theirs:,.0f} "
            f"by {gap:+.0%}",
            (item.prov.ref, "SEC company facts (comps read)"),
            f"recheck the {name} extraction (definitions, period)",
            "data",
        )


def review(state: dict[str, Any], revision: int = 0) -> CritiqueReport:
    """The deterministic review of a valued session."""
    issues = Issues()
    bundle = state.get("data")
    wacc: WACCResult | None = state.get("wacc")
    scenarios: ForecastScenarios | None = state.get("forecast")
    valuation: Valuation | None = state.get("valuation")
    comps: CompsResult | None = state.get("comps")
    if wacc is None or scenarios is None or valuation is None or bundle is None:
        issues.add(
            "incomplete",
            "high",
            "no valuation to review",
            ("dcf_agent.agents.critic",),
            "value the company first",
            "forecast",
        )
        return CritiqueReport(
            issues=tuple(issues.items), score=1, passed=False, revision_count=revision
        )
    rf = wacc.rf.value
    if not rf <= wacc.wacc.value <= WACC_CEILING:
        issues.add(
            "wacc",
            "high",
            f"WACC {wacc.wacc.value:.2%} outside {rf:.2%} (risk-free) to {WACC_CEILING:.0%}",
            (wacc.wacc.prov.ref, wacc.rf.prov.ref),
            "recheck the beta, the premiums and the weights",
            "wacc",
        )
    if wacc.cost_equity.value < rf:
        issues.add(
            "cost_of_equity",
            "high",
            f"cost of equity {wacc.cost_equity.value:.2%} below the risk-free rate {rf:.2%}",
            (wacc.cost_equity.prov.ref,),
            "recheck the beta and the equity risk premium",
            "wacc",
        )
    beta = wacc.relevered_beta.value
    if not BETA_RANGE[0] <= beta <= BETA_RANGE[1]:
        issues.add(
            "beta",
            "medium",
            f"beta {beta:.2f} outside {BETA_RANGE[0]}-{BETA_RANGE[1]}",
            (wacc.relevered_beta.prov.ref,),
            "recheck the industry or the regression window",
            "wacc",
        )
    industry = industry_benchmarks(bundle)
    industry_margin = industry.margin.value if industry and industry.margin else None
    normalized = state.get("normalized")
    history = driver_stats(normalized) if normalized is not None and normalized.annual else None
    for item in scenarios.scenarios:
        scenario_checks(item, wacc, issues, history, industry_margin)
    for result in valuation.results:
        share = result.tv_share_of_ev
        if share is not None and share > TV_SHARE:
            issues.add(
                "tv_share",
                "medium",
                f"{result.scenario}: terminal value {share:.0%} of enterprise value"
                + (" (the explicit years' cash flows are negative)" if share > 1 else ""),
                (result.calculation_ref,),
                "check the terminal assumptions; the explicit years carry little value",
                "forecast",
            )
    if (
        comps is not None
        and comps.divergence_vs_dcf is not None
        and abs(comps.divergence_vs_dcf) > COMPS_GAP
    ):
        issues.add(
            "comps",
            "medium",
            f"DCF value {comps.divergence_vs_dcf:+.0%} from the comps' implied value",
            (comps.calculation_ref, *(p.ticker for p in comps.peers[:3])),
            "explain the gap (growth, margins, risk) or revisit the forecast",
            "forecast",
        )
    price = valuation.price
    notes = state.get("forecast_notes", ())
    if price:
        gap = valuation.weighted / price - 1
        explained = any(n.startswith("base value") and "explained" in n for n in notes)
        if abs(gap) > PRICE_GAP and not explained:
            issues.add(
                "price",
                "medium",
                f"value {gap:+.0%} from the market price, not explained",
                ("market price",),
                "explain the gap in the base story",
                "forecast",
            )
    base = valuation.scenario("base")
    target = state.get("comps_target")  # the filing's figures, read independently (comps)
    if base is not None and target:
        bridge_checks(base, target, bundle, issues)
    parity = state.get("parity")
    if parity is not None and not parity.startswith(("8/8", "no error")):
        issues.add(
            "parity",
            "high",
            f"workbook differs from the engine: {parity}",
            (valuation.workbook or "workbook",),
            "rebuild the workbook",
            "reporter",
        )
    audit = audit_state(state)
    for problem in audit.problems[:5]:
        issues.add(
            "provenance",
            "high",
            f"unsourced value: {problem[:200]}",
            ("dcf_agent.engine.audit",),
            "attach the source of this value",
            "data",
        )
    high = any(i.severity == "high" for i in issues.items)
    score = max(
        1.0,
        5.0
        - 1.5 * sum(i.severity == "high" for i in issues.items)
        - 0.5 * sum(i.severity == "medium" for i in issues.items),
    )
    return CritiqueReport(
        issues=tuple(issues.items), score=score, passed=not high, revision_count=revision
    )


# The rubric (one call of a separate model): it scores and adds concerns, never clears an issue.
PROMPT_VERSION = "critic-rubric-v1"
RUBRIC = (
    "narrative_consistency",
    "evidence_quality",
    "scenario_distinctness",
    "unsupported_claims",
    "missing_risks",
)


class Concern(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1, max_length=80)  # an ID from the evidence list
    severity: Literal["low", "medium"]


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    narrative_consistency: int = Field(ge=1, le=5)
    evidence_quality: int = Field(ge=1, le=5)
    scenario_distinctness: int = Field(ge=1, le=5)
    unsupported_claims: int = Field(ge=1, le=5)
    missing_risks: int = Field(ge=1, le=5)
    concerns: list[Concern] = Field(default_factory=list, max_length=3)


STORY_REF = "dcf_agent.agents.forecast (scenario story)"
PREAMBLE = re.compile(r"(?i)carefully consider|in addition to the other information")


def evidence_list(state: dict[str, Any], risks: Sequence[tuple[str, str]]) -> dict[str, str]:
    """IDs the rubric may cite: the scenario stories and the risk-factor passages."""
    scenarios: ForecastScenarios | None = state.get("forecast")
    ids = {
        f"{item.scenario}.story": STORY_REF
        for item in (scenarios.scenarios if scenarios is not None else ())
    }
    for k, (ref, _) in enumerate(risks, start=1):
        ids[f"risk{k}"] = ref
    return ids


def risk_passages(hits: Sequence[tuple[str, str]], limit: int = 3) -> list[tuple[str, str]]:
    """Risk-factor passages without Item 1A's preamble."""
    return [(ref, text) for ref, text in hits if not PREAMBLE.search(text[:300])][:limit]


def rubric_prompt(
    state: dict[str, Any], report: CritiqueReport, risks: Sequence[tuple[str, str]]
) -> str:
    scenarios: ForecastScenarios = state["forecast"]
    valuation: Valuation | None = state.get("valuation")
    lines = []
    for item in scenarios.scenarios:
        value = valuation.scenario(item.scenario) if valuation is not None else None
        per_share = f", {value.value_per_share:,.2f} per share" if value is not None else ""
        growth = ", ".join(f"{g.value:.1%}" for g in item.revenue_growth[:5])
        margin = ", ".join(f"{m.value:.1%}" for m in item.operating_margin[:5])
        lines.append(
            f"{item.scenario}.story ({item.probability:.0%}{per_share}; revenue growth years "
            f"1-5: {growth}, fading to the terminal growth {item.terminal_growth.value:.1%} "
            f"by year {len(item.year_ends)}; operating margin years 1-5: {margin}): "
            f"{item.narrative[:450]}"
        )
    found = "\n".join(f"- {i.description}" for i in report.issues) or "- none"
    risk_text = "\n".join(f"risk{k}: {text[:260]}" for k, (_, text) in enumerate(risks, 1))
    ids = ", ".join(evidence_list(state, risks))
    return (
        "You review the stories of a DCF valuation's three scenarios. The numbers come from "
        "code under a fixed policy and are already checked: do not question the policy or "
        "the arithmetic. Judge the stories only. Score from 1 (poor) to 5 (good): "
        "narrative_consistency (each story matches its own numbers), evidence_quality (the "
        "stories rest on the company's own facts), scenario_distinctness (bear, base and "
        "bull are different worlds), unsupported_claims (5 = no claim without support), "
        "missing_risks (5 = the stories cover the risk factors below). Add at most three "
        f"concerns the checks did not find, each citing one ID from: {ids}.\n\n"
        + "\n".join(lines)
        + f"\n\nChecks already found:\n{found}\n\nRisk factors from the 10-K:\n"
        + (risk_text or "-")
    )


def with_rubric(report: CritiqueReport, rubric: Rubric, ids: dict[str, str]) -> CritiqueReport:
    """The deterministic report plus the rubric's scores and its cited concerns."""
    issues = Issues()
    issues.items = list(report.issues)
    for concern in rubric.concerns:
        ref = ids.get(concern.evidence.strip())
        if ref is None:  # a concern without valid evidence is dropped
            continue
        issues.add(
            "rubric",
            concern.severity,
            concern.description,
            (f"{concern.evidence}: {ref}",),
            "address it in the scenario's story, or its drivers at Gate 2",
            "forecast",
        )
    scores = {name: getattr(rubric, name) for name in RUBRIC}
    added = len(issues.items) - len(report.issues)
    mean = sum(scores.values()) / len(scores)
    score = max(1.0, min(5.0, (report.score + mean) / 2 - 0.25 * added))
    return CritiqueReport(
        issues=tuple(issues.items),
        score=round(score, 2),
        rubric_scores=scores,
        passed=report.passed,
        revision_count=report.revision_count,
    )


def run_rubric(
    llm: LocalLLM,
    state: dict[str, Any],
    report: CritiqueReport,
    risks: Sequence[tuple[str, str]] = (),
) -> CritiqueReport:
    """The rubric call; the deterministic report unchanged if the model fails."""
    if state.get("forecast") is None:
        return report
    try:
        reply = llm.generate(
            "critic", rubric_prompt(state, report, risks), Rubric, prompt_version=PROMPT_VERSION
        ).value
    except LLMError:
        return report
    return with_rubric(report, reply, evidence_list(state, risks))


# Repairs: mechanical fixes in code, never over a value the user set at Gate 2.
def repair(
    scenarios: ForecastScenarios, report: CritiqueReport, wacc: WACCResult
) -> tuple[ForecastScenarios, list[str]]:
    """Terminal growth capped and the last tax rate set to marginal where the Critic found them."""
    flagged = {i.issue_id.split(":")[0] for i in report.issues if i.severity == "high"}
    cap = min(wacc.rf.value, GDP_CAP)
    changes: list[str] = []
    fixed: list[ForecastAssumptions] = []
    for item in scenarios.scenarios:
        update: dict[str, Any] = {}
        g = item.terminal_growth
        if "terminal_growth" in flagged and g.value > cap and g.prov.source != "user_override":
            update["terminal_growth"] = fix(g, cap, f"capped at min(risk-free, {GDP_CAP:.0%})")
            changes.append(f"{item.scenario}: terminal growth {g.value:.2%} -> {cap:.2%}")
        last, marginal = item.tax_rate[-1], item.marginal_tax_rate.value
        if "tax" in flagged and last.value != marginal and last.prov.source != "user_override":
            update["tax_rate"] = (*item.tax_rate[:-1], fix(last, marginal, "set to marginal"))
            changes.append(f"{item.scenario}: last tax {last.value:.1%} -> {marginal:.1%}")
        fixed.append(item.model_copy(update=update) if update else item)
    if not changes:
        return scenarios, []
    return ForecastScenarios(scenarios=tuple(fixed)), changes


def fix(item: Sourced[float], value: float, how: str) -> Sourced[float]:
    return item.model_copy(
        update={
            "value": value,
            "prov": item.prov.model_copy(
                update={
                    "source": "derived",
                    "ref": f"{item.prov.ref}; dcf_agent.agents.critic",
                    "note": f"Critic fix: {how} (was {item.value:.4f}); {item.prov.note or ''}",
                }
            ),
        }
    )


def forecast_note(report: CritiqueReport) -> str | None:
    """The Critic's high-severity forecast issues as a note for a re-think."""
    fixes = [
        f"{i.description}; fix: {i.suggested_fix}"
        for i in report.issues
        if i.severity == "high" and i.route_back_to == "forecast"
    ]
    return ("Critic review: " + " | ".join(fixes))[:1500] if fixes else None
