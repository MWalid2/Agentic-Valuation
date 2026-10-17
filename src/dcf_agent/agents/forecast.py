"""Forecast Agent: bear, base and bull scenarios by Tree-of-Thought, the model choosing only.

1. Anchors (code): starting growth (guidance, otherwise recent growth blended with
   the CAGR), starting clean margin (TTM, otherwise latest), sales-to-capital (the
   company's own unless implausible against its industry, then the industry's),
   tax fading from the effective to the marginal rate, terminal growth from the
   risk-free rate.
2. Axes (one constrained model call): the model picks a level per axis from fixed
   choices and cites one line of evidence for each. No number comes from the model.
3. Worlds (code): every growth x margin combination becomes year-by-year drivers by
   fade rules; worlds that fail a hard bound are dropped; the rest are scored
   (soft flags, distance from the chosen levels) and previewed with the engine.
4. Scenarios (code): base is the chosen world (or the best scored), bear and bull its
   neighbours one level down and up; probabilities 25/50/25.
5. Stories (one model call): three short narratives, written after the numbers are
   fixed. Only then is the market price shown, so a large gap can be explained but
   never tuned away (anti-anchoring).
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import product
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from dcf_agent.contracts.accounting import FilingItem, NormalizedFinancials
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.modeling import (
    ForecastAssumptions,
    ForecastScenarios,
    Scenario,
    WACCResult,
)
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.engine.assemble import Assembled
from dcf_agent.engine.drivers import (
    GDP_CAP,
    Benchmarks,
    Coherence,
    DriverStats,
    Preview,
    check_coherence,
    driver_stats,
    fade_path,
    forecast_years,
    industry_benchmarks,
    quick_dcf,
)
from dcf_agent.llm import LLMError, LocalLLM

PROMPT_VERSION = "forecast-v2"
HORIZON = 10
MARGIN_YEARS = 5  # margins reach their target in five years, then hold
TERMINAL_CAP = 0.03  # base terminal growth: min(risk-free rate, 3%)
TERMINAL_STEP = 0.005  # bear and bull terminal growth: 0.5 points below and above base
PROBABILITIES: dict[Scenario, float] = {"bear": 0.25, "base": 0.5, "bull": 0.25}
ANCHORING_GAP = 0.5  # base value this far from the price needs an explanation
SPREAD = 0.15  # bear and bull at least this far from base in value, when a world allows
SCENARIO_TURNOVER: dict[Scenario, float] = {"bear": 0.85, "base": 1.0, "bull": 1.15}
MARGIN_STEP = 0.03  # the smallest margin move of a compressing or expanding trajectory
BEYOND_GROWTH = 0.02  # starting growth step of a scenario past the edge of the grid
Growth = Literal["fades_fast", "fades_normally", "durable"]
Margin = Literal["compresses", "holds", "expands"]
Reinvestment = Literal["below_history", "history", "industry"]
Moat = Literal["none", "narrow", "wide"]
GROWTHS: tuple[Growth, ...] = ("fades_fast", "fades_normally", "durable")
MARGINS: tuple[Margin, ...] = ("compresses", "holds", "expands")
MOAT_SPREAD: dict[Moat, float] = {"none": 0.0, "narrow": 0.02, "wide": 0.05}  # minimums
# Share of the company's historical excess return (median ROIC over WACC) kept in the long
# run, and its ceiling, by moat level.
MOAT_KEPT: dict[Moat, tuple[float, float]] = {
    "none": (0.0, 0.0),
    "narrow": (0.5, 0.06),
    "wide": (0.75, 0.15),
}


def terminal_spread(moat: Moat, historical_roic: float | None, terminal_wacc: float) -> float:
    """Long-run return above WACC: a share of the company's own excess, within bounds."""
    kept, ceiling = MOAT_KEPT[moat]
    excess = max(0.0, (historical_roic or terminal_wacc) - terminal_wacc)
    return min(max(MOAT_SPREAD[moat], kept * excess), ceiling) if moat != "none" else 0.0


HOLD: dict[Growth, tuple[int, Literal["linear", "convex"]]] = {
    "fades_fast": (0, "convex"),
    "fades_normally": (2, "linear"),
    "durable": (4, "linear"),
}


class AxisChoice(BaseModel):
    """The model's one call: a level per axis, each with its evidence."""

    model_config = ConfigDict(extra="forbid")
    growth: Growth
    growth_evidence: str = Field(max_length=1200)
    margin: Margin
    margin_evidence: str = Field(max_length=1200)
    reinvestment: Reinvestment
    reinvestment_evidence: str = Field(max_length=1200)
    moat: Moat
    moat_evidence: str = Field(max_length=1200)


class Stories(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bear: str = Field(max_length=600)
    base: str = Field(max_length=600)
    bull: str = Field(max_length=600)


@dataclass(frozen=True)
class Anchors:
    """The numbers every world starts from, each with how it was set."""

    growth: float
    margin: float
    sales_to_capital: float
    tax_start: float
    marginal_tax: float
    terminal_growth: float
    terminal_wacc: float
    risk_free: float
    notes: dict[str, str]
    guided_margin: float | None = None


@dataclass
class World:
    growth: Growth
    margin: Margin
    assumptions: ForecastAssumptions | None = None
    coherence: Coherence | None = None
    preview: Preview | None = None
    score: float = 0.0

    @property
    def key(self) -> tuple[int, int]:
        return GROWTHS.index(self.growth), MARGINS.index(self.margin)

    @property
    def value(self) -> float | None:
        return self.preview.per_share if self.preview is not None else None


@dataclass
class Forecast:
    scenarios: ForecastScenarios | None
    choice: AxisChoice
    chosen_by: Literal["model", "default"]
    anchors: Anchors
    worlds: list[World]
    picked: dict[Scenario, World]
    notes: list[str] = field(default_factory=list)
    model_calls: int = 0


def guidance_rate(items: Sequence[FilingItem], metrics: tuple[str, ...]) -> FilingItem | None:
    for metric in metrics:
        for item in items:
            if item.item_id.startswith(f"guidance:{metric}:") and item.amount is not None:
                return item
    return None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def anchors(
    stats: DriverStats,
    industry: Benchmarks | None,
    wacc: WACCResult,
    guidance: Sequence[FilingItem],
) -> Anchors:
    notes: dict[str, str] = {}
    guided = guidance_rate(guidance, ("revenue_growth", "organic_growth"))
    recent = stats.ttm_growth if stats.ttm_growth is not None else stats.growth.latest
    if guided is not None and guided.amount is not None:
        growth = guided.amount.value
        notes["growth"] = f"guidance: {guided.description[:160]}"
    elif recent is not None and stats.cagr is not None:
        growth = 0.5 * recent + 0.5 * stats.cagr
        notes["growth"] = f"half the latest growth ({recent:.1%}), half the CAGR ({stats.cagr:.1%})"
    else:
        growth = stats.cagr or 0.03
        notes["growth"] = "the revenue CAGR" if stats.cagr is not None else "3%: no history"
    growth = clamp(growth, -0.2, 0.6)
    margin = stats.ttm_margin if stats.ttm_margin is not None else (stats.margin.latest or 0.0)
    notes["margin"] = (
        f"clean operating margin, {'TTM' if stats.ttm_margin is not None else 'latest year'} "
        f"{margin:.1%}"
    )
    own = stats.sales_to_capital.median
    peer = industry.sales_to_capital.value if industry and industry.sales_to_capital else None
    if own is not None and own > 0 and (peer is None or peer <= own <= 2.5 * peer):
        turnover = own
        notes["sales_to_capital"] = f"the company's median sales to invested capital {own:.2f}"
    elif peer is not None and own is not None and own > 2.5 * peer:
        turnover = 2.5 * peer
        notes["sales_to_capital"] = (
            f"2.5 x the industry's {peer:.2f}: the company's {own:.2f} reflects very little "
            "invested capital, not the capital new growth needs"
        )
    elif peer is not None:
        turnover = peer
        notes["sales_to_capital"] = f"the industry's {peer:.2f}" + (
            f": the company's {own:.2f} includes acquired goodwill, more than new growth needs"
            if own
            else ""
        )
    else:
        turnover = 1.5
        notes["sales_to_capital"] = "1.5: no history or industry figure"
    marginal = stats.marginal_tax if stats.marginal_tax is not None else 0.25
    effective = stats.effective_tax.median
    guided_tax = guidance_rate(guidance, ("tax_rate",))
    if guided_tax is not None and guided_tax.amount is not None:
        tax_start = clamp(guided_tax.amount.value, 0.0, 0.5)
        notes["tax"] = f"guidance {tax_start:.1%}, converging to the marginal {marginal:.1%}"
    else:
        tax_start = clamp(effective if effective is not None else marginal, 0.0, 0.5)
        notes["tax"] = (
            f"median effective {tax_start:.1%}, converging to the marginal {marginal:.1%}"
        )
    rf = wacc.rf.value
    terminal = min(rf, TERMINAL_CAP)
    notes["terminal_growth"] = f"min(risk-free {rf:.2%}, {TERMINAL_CAP:.0%})"
    guided = guidance_rate(guidance, ("operating_margin",))
    guided_margin = guided.amount.value if guided is not None and guided.amount else None
    if guided_margin is not None:
        notes["margin"] += f"; guided operating margin {guided_margin:.1%}"
    return Anchors(
        growth,
        margin,
        turnover,
        tax_start,
        marginal,
        terminal,
        wacc.terminal_wacc.value,
        rf,
        notes,
        guided_margin,
    )


def margin_target(
    level: Margin,
    start: float,
    industry: Benchmarks | None,
    high: float | None,
    guided: float | None = None,
) -> float:
    """Where the margin goes under each trajectory (in five years; loss makers over ten)."""
    peer = industry.margin.value if industry and industry.margin else None
    if start <= 0:
        # Loss makers converge toward the better of the industry and their own guidance.
        base = max(m for m in (peer, guided, 0.05) if m is not None)
        return {"compresses": 0.5 * base, "holds": base, "expands": 1.25 * base}[level]
    if level == "holds":
        return start
    if peer is None:
        return start - MARGIN_STEP if level == "compresses" else start + MARGIN_STEP
    gap = abs(start - peer)
    if start >= peer:
        # Above the industry: compressing moves part of the way toward it; expanding is
        # small and stays near the company's own best year.
        if level == "compresses":
            return start - max(MARGIN_STEP, 0.25 * gap)
        return min(start + MARGIN_STEP, max(start, high or start) + 0.015)
    # Below the industry: expanding closes part of the gap, compressing widens it a little.
    if level == "compresses":
        return start - MARGIN_STEP
    return min(start + max(MARGIN_STEP, 0.5 * gap), peer + 0.02)


def rule_choice(
    stats: DriverStats, wacc: WACCResult, a: Anchors, industry: Benchmarks | None
) -> AxisChoice:
    """The level each axis takes from the statistics alone (the model may depart, if supported)."""
    recent = stats.ttm_growth if stats.ttm_growth is not None else stats.growth.latest
    cagr = stats.cagr
    growth: Growth = "fades_normally"
    why_growth = "recent growth in line with the CAGR"
    if recent is not None and cagr is not None:
        if recent < cagr - 0.02:
            growth, why_growth = (
                "fades_fast",
                f"recent growth {recent:.1%} below the CAGR {cagr:.1%}",
            )
        elif recent > cagr + 0.02 and (stats.growth.volatility or 0) < 0.1:
            growth, why_growth = "durable", f"recent growth {recent:.1%} above the CAGR {cagr:.1%}"
    trend = stats.margin.trend
    margin: Margin = "holds"
    why_margin = "no clear margin trend"
    if trend is not None and trend < -0.005:
        margin, why_margin = "compresses", f"margin falling {trend:.1%} a year"
    elif trend is not None and trend > 0.005:
        margin, why_margin = "expands", f"margin rising {trend:.1%} a year"
    roic = stats.roic.median
    spread = roic - wacc.wacc.value if roic is not None else 0.0
    moat: Moat = "wide" if spread > 0.15 else "narrow" if spread > 0.05 else "none"
    return AxisChoice(
        growth=growth,
        growth_evidence=f"rule: {why_growth}",
        margin=margin,
        margin_evidence=f"rule: {why_margin}",
        reinvestment="history",
        reinvestment_evidence=f"rule: {a.notes['sales_to_capital']}",
        moat=moat,
        moat_evidence=f"rule: median ROIC {roic:.1%} against WACC {wacc.wacc.value:.1%}"
        if roic is not None
        else "rule: no ROIC history",
    )


def supported(
    axis: str,
    level: str,
    stats: DriverStats,
    wacc: WACCResult,
    a: Anchors,
    industry: Benchmarks | None,
) -> bool:
    """Whether the facts support a level that departs from the rule."""
    recent = stats.ttm_growth if stats.ttm_growth is not None else stats.growth.latest
    cagr = stats.cagr or 0.0
    trend = stats.margin.trend or 0.0
    peer = industry.margin.value if industry and industry.margin else None
    roic = stats.roic.median
    if axis == "growth":
        if level == "fades_fast":
            return (recent is not None and recent < cagr) or (stats.growth.volatility or 0) > 0.1
        if level == "durable":
            return recent is not None and recent >= cagr and (stats.growth.volatility or 0) < 0.1
        return True  # fading normally is never implausible
    if axis == "margin":
        if level == "compresses":
            return (
                trend < 0
                or (peer is not None and a.margin - peer > 0.10)
                or (a.guided_margin is not None and a.guided_margin < a.margin)
            )
        if level == "expands":
            return (
                trend > 0
                or (peer is not None and a.margin < peer - 0.05)
                or (a.guided_margin is not None and a.guided_margin > a.margin)
            )
        return True
    if axis == "moat" and roic is not None:
        spread = roic - wacc.wacc.value
        return {"none": spread < 0.05, "narrow": -0.02 < spread, "wide": spread > 0.10}[level]
    return True


def reconcile(
    choice: AxisChoice | None,
    rule: AxisChoice,
    stats: DriverStats,
    wacc: WACCResult,
    a: Anchors,
    industry: Benchmarks | None,
) -> tuple[AxisChoice, list[str]]:
    """The model's levels where it agrees with the rule or the facts support it; else the rule."""
    if choice is None:
        return rule, []
    kept: dict[str, str] = {}
    rejected = []
    for axis in ("growth", "margin", "reinvestment", "moat"):
        level = getattr(choice, axis)
        evidence = getattr(choice, f"{axis}_evidence")[:300]
        if level == getattr(rule, axis) or supported(axis, level, stats, wacc, a, industry):
            kept[axis], kept[f"{axis}_evidence"] = level, evidence
        else:
            kept[axis] = getattr(rule, axis)
            kept[f"{axis}_evidence"] = getattr(rule, f"{axis}_evidence")
            rejected.append(
                f"{axis}: the model chose {level} ({evidence[:120]}), not supported by the "
                f"statistics; the rule's {getattr(rule, axis)} applies"
            )
    return AxisChoice.model_validate(kept), rejected


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def evidence_prompt(
    name: str,
    stats: DriverStats,
    industry: Benchmarks | None,
    wacc: WACCResult,
    guidance: Sequence[FilingItem],
) -> str:
    years = ", ".join(f"FY{d.year} growth {pct(g)}" for d, g in stats.growth.values)
    margins = ", ".join(f"FY{d.year} {pct(m)}" for d, m in stats.margin.values)
    lines = [
        f"Company: {name}.",
        f"Revenue: {years}; CAGR {pct(stats.cagr)}; latest TTM growth {pct(stats.ttm_growth)}.",
        f"Clean operating margin: {margins}; TTM {pct(stats.ttm_margin)}; trend per year "
        f"{pct(stats.margin.trend)}.",
        f"Sales to invested capital: median {stats.sales_to_capital.median or 0:.2f}. ROIC median "
        f"{pct(stats.roic.median)}. WACC {pct(wacc.wacc.value)}.",
    ]
    if industry is not None:
        lines.append(
            f"Industry ({industry.industry}): operating margin "
            f"{pct(industry.margin.value if industry.margin else None)}, sales to capital "
            f"{industry.sales_to_capital.value if industry.sales_to_capital else 0:.2f}."
        )
    lines.append(
        "Management guidance: "
        + ("; ".join(g.description[:160] for g in guidance) if guidance else "none given.")
    )
    lines.append(
        "Choose one level per axis and cite the evidence from the facts above for each, in "
        "fewer than 20 words.\n"
        "growth (how long revenue growth lasts before fading to the long-run rate): fades_fast, "
        "fades_normally, or durable.\n"
        "margin (operating margin over the next five years): compresses, holds, or expands.\n"
        "reinvestment (capital needed per dollar of growth): below_history (needs more capital "
        "than in the past), history, or industry.\n"
        "moat (returns above the cost of capital in the long run): none, narrow, or wide."
    )
    return "\n".join(lines)


def sourced(value: float, end: date | None, as_of: date, note: str, refs: str) -> Sourced[float]:
    return Sourced[float](
        value=value,
        prov=Provenance(
            source="derived",
            ref=refs,
            as_of=as_of,
            period_end=end,
            confidence="medium",
            note=note[:500],
        ),
    )


POLICY = "policy: docs/25-phase-4-plan.md, decisions and step 3"


def driver_sources(
    normalized: NormalizedFinancials,
    industry: Benchmarks | None,
    guidance: Sequence[FilingItem],
    wacc: WACCResult,
    a: Anchors,
) -> dict[str, str]:
    """For each driver, the references of the inputs it is built from (and the policy)."""
    years = sorted(normalized.annual, key=lambda p: p.period_end)
    latest = normalized.ttm or years[-1]

    def join(*refs: str | None) -> str:
        return " | ".join(dict.fromkeys(r for r in refs if r))[:1000]

    guided = guidance_rate(guidance, ("revenue_growth", "organic_growth"))
    growth = (
        join(guided.amount.prov.ref if guided and guided.amount else None, POLICY)
        if a.notes["growth"].startswith("guidance")
        else join(years[-1].revenue.prov.ref, years[0].revenue.prov.ref, POLICY)
    )
    guided_tax = guidance_rate(guidance, ("tax_rate",))
    tax = (
        guided_tax.amount.prov.ref
        if a.notes["tax"].startswith("guidance") and guided_tax and guided_tax.amount
        else years[-1].effective_tax_rate.prov.ref
    )
    turnover = (
        industry.sales_to_capital.prov.ref
        if industry and industry.sales_to_capital and "industry" in a.notes["sales_to_capital"]
        else (years[-1].sales_to_capital.prov.ref if years[-1].sales_to_capital else None)
    )
    roic = years[-1].roic.prov.ref if years[-1].roic else None
    return {
        "growth": growth,
        "margin": join(latest.clean_ebit.prov.ref, latest.revenue.prov.ref, POLICY),
        "sales_to_capital": join(turnover, POLICY),
        "tax": join(tax, latest.marginal_tax_rate.prov.ref),
        "marginal_tax": join(latest.marginal_tax_rate.prov.ref),
        "terminal_growth": join(wacc.rf.prov.ref, POLICY),
        "terminal_roic": join(wacc.terminal_wacc.prov.ref, roic, POLICY),
    }


def build(
    scenario: Scenario,
    growth: Growth,
    margin: Margin,
    choice: AxisChoice,
    a: Anchors,
    stats: DriverStats,
    industry: Benchmarks | None,
    ends: tuple[date, ...],
    as_of: date,
    refs: dict[str, str],
    beyond: int = 0,
) -> ForecastAssumptions:
    """One world's year-by-year drivers, every value sourced and explained.

    `beyond` (-1 or 1) extends a bear or bull past the edge of the grid when its world
    is already at the lowest (highest) level: a further margin step and two points of
    starting growth.
    """
    shift = {"bear": -TERMINAL_STEP, "base": 0.0, "bull": TERMINAL_STEP}[scenario]
    # Never above the risk-free rate or the long-run cap (the hard bounds).
    terminal = min(a.terminal_growth + shift, a.risk_free, GDP_CAP)
    hold, shape = HOLD[growth]
    if a.growth > 0.25:
        # Very high growth decays fast: the hold shortens and the fade is convex.
        hold, shape = max(0, hold - 1), "convex"
    start = a.growth + BEYOND_GROWTH * beyond
    growth_path = fade_path(start, terminal, HORIZON, hold=hold, shape=shape)
    target = margin_target(margin, a.margin, industry, stats.margin.high, a.guided_margin)
    target += MARGIN_STEP * beyond
    peer = industry.margin.value if industry and industry.margin else None
    if beyond > 0 and peer is not None and a.margin >= peer:
        # Already above its industry: the bull's extra step stays near its own best year.
        target = min(target, max(a.margin, stats.margin.high or a.margin) + 0.015)
    years = HORIZON if a.margin <= 0 else MARGIN_YEARS
    margin_path = fade_path(a.margin, target, years) + (target,) * (HORIZON - years)
    factor = {"below_history": 0.8, "history": 1.0, "industry": 1.0}[choice.reinvestment]
    turnover = a.sales_to_capital * factor
    if choice.reinvestment == "industry" and industry and industry.sales_to_capital:
        turnover = industry.sales_to_capital.value
    # Bear needs more capital per dollar of growth, bull less.
    turnover *= SCENARIO_TURNOVER[scenario]
    tax_path = fade_path(a.tax_start, a.marginal_tax, HORIZON)
    # Long-run returns are the main uncertainty of a mature company: bear one moat level
    # lower, bull one higher.
    moats: tuple[Moat, ...] = ("none", "narrow", "wide")
    step = {"bear": -1, "base": 0, "bull": 1}[scenario]
    moat = moats[int(clamp(moats.index(choice.moat) + step, 0, 2))]
    spread = terminal_spread(moat, stats.roic.median, a.terminal_wacc)
    roic = a.terminal_wacc + spread
    note = f"{scenario}: growth {growth}, margin {margin}" + (
        f", one step {'below' if beyond < 0 else 'above'} the grid" if beyond else ""
    )
    rationale = {
        "growth": f"{growth}: {a.notes['growth']}; held {hold} years then a {shape} fade to "
        f"{terminal:.2%}. Evidence: {choice.growth_evidence}",
        "margin": f"{margin}: from {a.notes['margin']} to {target:.1%} in {MARGIN_YEARS} years. "
        f"Evidence: {choice.margin_evidence}",
        "reinvestment": f"{choice.reinvestment}: {a.notes['sales_to_capital']}; used "
        f"{turnover:.2f}. Evidence: {choice.reinvestment_evidence}",
        "tax": a.notes["tax"],
        "terminal_growth": a.notes["terminal_growth"]
        + (f", {shift:+.1%} in this scenario" if shift else ""),
        "terminal_roic": f"moat {moat}"
        + (
            ""
            if moat == choice.moat
            else f" (one level {'above' if step > 0 else 'below'}"
            f" the base world's {choice.moat}, by scenario)"
        )
        + f": terminal WACC + {spread:.1%} "
        f"(keeps {MOAT_KEPT[moat][0]:.0%} of the median ROIC's "
        f"{pct(stats.roic.median)} excess over WACC, within {MOAT_SPREAD[moat]:.0%}-"
        f"{MOAT_KEPT[moat][1]:.0%}). "
        f"Evidence: {choice.moat_evidence}",
    }
    if spread > 0.05:
        rationale["moat"] = f"{moat}: {choice.moat_evidence}"
    return ForecastAssumptions(
        as_of=as_of,
        scenario=scenario,
        probability=PROBABILITIES[scenario],
        year_ends=ends,
        revenue_growth=tuple(
            sourced(v, e, as_of, f"{note}: revenue growth; {rationale['growth']}", refs["growth"])
            for v, e in zip(growth_path, ends, strict=True)
        ),
        operating_margin=tuple(
            sourced(v, e, as_of, f"{note}: operating margin; {rationale['margin']}", refs["margin"])
            for v, e in zip(margin_path, ends, strict=True)
        ),
        sales_to_capital=tuple(
            sourced(
                turnover,
                e,
                as_of,
                f"{note}: sales to capital; {rationale['reinvestment']}",
                refs["sales_to_capital"],
            )
            for e in ends
        ),
        tax_rate=tuple(
            sourced(v, e, as_of, f"{note}: tax rate; {rationale['tax']}", refs["tax"])
            for v, e in zip(tax_path, ends, strict=True)
        ),
        terminal_growth=sourced(
            terminal,
            None,
            as_of,
            f"{note}: terminal growth; {rationale['terminal_growth']}",
            refs["terminal_growth"],
        ),
        terminal_roic=sourced(
            roic,
            None,
            as_of,
            f"{note}: terminal ROIC; {rationale['terminal_roic']}",
            refs["terminal_roic"],
        ),
        marginal_tax_rate=sourced(
            a.marginal_tax,
            None,
            as_of,
            "marginal tax rate from the filing's rate reconciliation (or the 25% policy)",
            refs["marginal_tax"],
        ),
        narrative=f"{scenario.capitalize()} case: growth {growth.replace('_', ' ')}, "
        f"margin {margin}.",
        rationale=rationale,
    )


ECHO = re.compile(r"^\W*(growth|margin)\b|value per share", re.I)


def story(item: ForecastAssumptions, written: str | None) -> str:
    """The model's story when it explains something; otherwise one built from the rationale."""
    if written and len(written.split()) >= 15 and not ECHO.search(written):
        return written
    growth = item.revenue_growth
    margin = item.operating_margin
    returns = re.split(r"\.\s", item.rationale["terminal_roic"])[0].rstrip(".")  # 1st sentence
    return (
        f"{item.scenario.capitalize()} case: revenue growth starts at {growth[0].value:.1%} and "
        f"fades to {item.terminal_growth.value:.1%} ({item.rationale['growth'].split(':')[0]}); "
        f"the operating margin moves from {margin[0].value:.1%} to {margin[-1].value:.1%}; "
        f"each dollar of new revenue needs {1 / item.sales_to_capital[0].value:.2f} dollars of "
        f"capital; long-run returns: {returns}."
    )


class ForecastAgent:
    def __init__(self, llm: LocalLLM | None) -> None:
        self.llm = llm

    def choose(self, prompt: str) -> AxisChoice | None:
        if self.llm is None:
            return None
        try:
            return self.llm.generate(
                "forecast", prompt, AxisChoice, prompt_version=PROMPT_VERSION
            ).value
        except LLMError:
            return None

    def stories(self, prompt: str) -> Stories | None:
        if self.llm is None:
            return None
        try:
            return self.llm.generate(
                "forecast", prompt, Stories, prompt_version=PROMPT_VERSION
            ).value
        except LLMError:
            return None

    def run(
        self,
        bundle: DataBundle,
        normalized: NormalizedFinancials,
        wacc: WACCResult,
        assembled: Assembled,
        note: str | None = None,
    ) -> Forecast:
        """note: the user's request at Gate 2 to reconsider a scenario, shown to the model."""
        as_of = bundle.request.valuation_date
        stats = driver_stats(normalized)
        industry = industry_benchmarks(bundle)
        insights = bundle.filing_insights
        guidance = insights.management_guidance if insights is not None else ()
        a = anchors(stats, industry, wacc, guidance)
        notes = list(stats.notes)
        calls = 0
        prompt = evidence_prompt(bundle.request.company_name, stats, industry, wacc, guidance)
        if note:
            prompt += f"\nThe user asks to reconsider: {note[:300]}"
        read = self.choose(prompt)
        calls += self.llm is not None
        chosen_by: Literal["model", "default"] = "model" if read is not None else "default"
        rule = rule_choice(stats, wacc, a, industry)
        choice, rejected = reconcile(read, rule, stats, wacc, a, industry)
        notes += [f"Gate 2: {r}" for r in rejected]
        last = max(normalized.annual, key=lambda p: p.period_end).period_end
        ends = forecast_years(last, as_of, HORIZON)
        refs = driver_sources(normalized, industry, guidance, wacc, a)
        worlds = []
        for growth, margin in product(GROWTHS, MARGINS):
            world = World(growth, margin)
            try:
                world.assumptions = build(
                    "base", growth, margin, choice, a, stats, industry, ends, as_of, refs
                )
            except ValueError as error:
                notes.append(f"{growth}/{margin}: not built ({error})")
                worlds.append(world)
                continue
            world.preview = quick_dcf(world.assumptions, wacc, assembled)
            world.coherence = check_coherence(
                world.assumptions,
                wacc,
                history=stats,
                industry=industry,
                preview=world.preview.result,
            )
            distance = abs(GROWTHS.index(growth) - GROWTHS.index(choice.growth)) + abs(
                MARGINS.index(margin) - MARGINS.index(choice.margin)
            )
            world.score = -distance - 0.5 * len(world.coherence.soft)
            worlds.append(world)
        valid = [w for w in worlds if w.coherence and w.coherence.valid and w.value is not None]
        if not valid:
            notes.append("no world passes the hard bounds")
            return Forecast(None, choice, chosen_by, a, worlds, {}, notes, calls)
        base = next(
            (w for w in valid if (w.growth, w.margin) == (choice.growth, choice.margin)),
            max(valid, key=lambda w: w.score),
        )
        g, m = base.key

        def neighbour(step: int) -> tuple[World, int]:
            """One level down (bear) or up (bull) on both axes, by structure, not by value.

            At the edge of the grid the scenario keeps that world and goes one step past it.
            The market price plays no part.
            """
            target = (int(clamp(g + step, 0, 2)), int(clamp(m + step, 0, 2)))
            found = next((w for w in valid if w.key == target), base)
            past = step if target == (g, m) or g + step != target[0] or m + step != target[1] else 0
            return found, past

        bear, bear_past = neighbour(-1)
        bull, bull_past = neighbour(1)
        picked: dict[Scenario, World] = {"bear": bear, "base": base, "bull": bull}
        beyond: dict[Scenario, int] = {"bear": bear_past, "base": 0, "bull": bull_past}
        built: list[ForecastAssumptions] = []
        for scenario, world in picked.items():
            item = build(
                scenario,
                world.growth,
                world.margin,
                choice,
                a,
                stats,
                industry,
                ends,
                as_of,
                refs,
                beyond[scenario],
            )
            built.append(item)
        # Stories after the numbers; the price only now, to explain a large gap.
        previews = {
            s: quick_dcf(f, wacc, assembled).per_share for s, f in zip(picked, built, strict=True)
        }
        price = bundle.market.close.price.value if bundle.market else None
        base_value = previews["base"]
        gap = base_value / price - 1 if price and base_value is not None else None
        story_prompt = (
            prompt
            + "\n\nThe scenarios are fixed (numbers by rule, not by you):\n"
            + "\n".join(
                f"{s}: growth {w.growth.replace('_', ' ')}, margin {w.margin}, value per share "
                f"{previews[s]:,.2f}"
                if previews[s] is not None
                else f"{s}: {w.growth}, {w.margin}"
                for s, w in picked.items()
            )
            + (
                f"\nThe market price is {price:,.2f}; the base value is {gap:+.0%} from it. "
                "Explain the main reason for this gap in the base story, from the facts above."
                if gap is not None and abs(gap) > ANCHORING_GAP and price is not None
                else ""
            )
            + "\nFor an investor, write three short stories (bear, base, bull), at most three "
            "sentences each, in plain language: what would have to happen in the business for "
            "revenue and margins to follow that path, using the facts above (history, industry, "
            "guidance). Do not repeat the labels or the values per share."
        )
        stories = self.stories(story_prompt)
        calls += self.llm is not None
        if gap is not None and abs(gap) > ANCHORING_GAP:
            notes.append(f"base value {gap:+.0%} from the market price: explained in the story")
        final = [
            f.model_copy(update={"narrative": story(f, getattr(stories, f.scenario, None))})
            for f in built
        ]
        try:
            scenarios = ForecastScenarios(scenarios=tuple(final))
        except ValueError as error:
            notes.append(f"scenarios not valid: {error}")
            scenarios = None
        return Forecast(scenarios, choice, chosen_by, a, worlds, picked, notes, calls)


def describe(forecast: Forecast) -> str:
    return json.dumps(
        {
            "choice": forecast.choice.model_dump(),
            "chosen_by": forecast.chosen_by,
            "worlds": [
                {"growth": w.growth, "margin": w.margin, "value": w.value, "score": w.score}
                for w in forecast.worlds
            ],
            "notes": forecast.notes,
        },
        indent=1,
    )
