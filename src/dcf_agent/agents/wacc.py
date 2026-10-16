"""WACC Agent: code measures the betas, the model may depart from the rule, the engine computes.

Code computes the bottom-up beta (industry unlevered beta relevered at market D/E),
two regressions against the market index (two years of weekly returns, the window
the CLI uses, and five years of monthly returns), which reliability thresholds each
window passes, the synthetic rating and the band. The model sees these facts and
returns only a method (bottom_up, regression or average) with one sentence.

A departure from the default rule must be supported by evidence code can verify,
as with the line classifier's guards: "regression" needs a window that meets the
reliability rule (its beta is then used); "average", a shrinkage of the regression
toward the industry, needs a window that explains the stock (R^2 meets the rule)
but is imprecise (only the standard error fails). An unsupported departure is
rejected and recorded; the rule applies.
The WACC is always recomputed by `engine.wacc`; an accepted departure is flagged
for review.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from dcf_agent.contracts.external import DataBundle, PriceHistory
from dcf_agent.contracts.modeling import WACCResult
from dcf_agent.data.cache import DataUnavailable
from dcf_agent.engine.assemble import assemble
from dcf_agent.engine.wacc import (
    MAX_STANDARD_ERROR,
    MIN_R_SQUARED,
    UNLEVERED_BETA,
    BetaMethod,
    Regression,
    WaccError,
    WaccInputs,
    choose_beta,
    compute_wacc,
    regression_beta,
    relever,
)
from dcf_agent.llm import LLMError, LocalLLM

PROMPT_VERSION = "wacc-beta-v3"
MARKET = "SPY"
LONG_WINDOW = timedelta(days=5 * 365 + 1)
PriceFetch = Callable[[str, date, date], PriceHistory]


class BetaChoice(BaseModel):
    method: BetaMethod
    reason: str = Field(min_length=1, max_length=400)


class WaccDecision(BaseModel):
    """What was chosen, by whom, and whether it departs from the default rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    method: BetaMethod
    rule_method: BetaMethod
    reason: str
    source: Literal["model", "rule"]
    flagged: bool  # the WACC departs from the default rule: review it
    rejected: str | None = None  # a model choice the evidence did not support
    evidence: tuple[str, ...]


def points(history: PriceHistory, start: date) -> list[tuple[date, float]]:
    return [(o.observed_on, o.price.value) for o in history.observations if o.observed_on >= start]


def reliable(fit: Regression) -> bool:
    return fit.r_squared >= MIN_R_SQUARED and fit.standard_error <= MAX_STANDARD_ERROR


def assessment(name: str, fit: Regression | None) -> str:
    if fit is None:
        return f"{name}: not available."
    r2 = "meets" if fit.r_squared >= MIN_R_SQUARED else "fails"
    se = "meets" if fit.standard_error <= MAX_STANDARD_ERROR else "fails"
    verdict = "reliable under the rule" if reliable(fit) else "not reliable under the rule"
    return (
        f"{name}: beta {fit.beta:.2f}; R^2 {fit.r_squared:.2f} ({r2} >= {MIN_R_SQUARED}); "
        f"standard error {fit.standard_error:.2f} ({se} <= {MAX_STANDARD_ERROR}); {verdict}."
    )


class WaccAgent:
    def __init__(
        self,
        prices: PriceFetch,
        llm: LocalLLM | None = None,
        *,
        marginal_tax: float = 0.25,
        market: str = MARKET,
    ) -> None:
        self.prices, self.llm = prices, llm
        self.marginal_tax, self.market = marginal_tax, market

    def regressions(
        self, bundle: DataBundle
    ) -> tuple[Regression | None, Regression | None, list[str]]:
        """(weekly two-year, monthly five-year, problems) against the market index."""
        issues: list[str] = []
        as_of = bundle.request.valuation_date
        weekly = monthly = None
        if bundle.prices is None:
            return None, None, ["No company price history: no regression"]
        start = bundle.prices.start
        try:
            index = self.prices(self.market, start, as_of)
            weekly = regression_beta(points(bundle.prices, start), points(index, start))
        except (DataUnavailable, WaccError) as error:
            issues.append(f"Weekly regression unavailable: {error}")
        long_start = as_of - LONG_WINDOW
        try:
            stock = self.prices(bundle.request.ticker, long_start, as_of)
            index = self.prices(self.market, long_start, as_of)
            monthly = regression_beta(
                points(stock, long_start), points(index, long_start), frequency="monthly"
            )
        except (DataUnavailable, WaccError) as error:
            issues.append(f"Monthly regression unavailable: {error}")
        return weekly, monthly, issues

    def prompt(
        self,
        inputs: WaccInputs,
        bottom_up: float,
        weekly: Regression | None,
        monthly: Regression | None,
        rule: str,
    ) -> str:
        de = inputs.debt_value.value / inputs.equity_value.value
        return "\n".join(
            [
                "Choose the beta method for a company's cost of equity.",
                "bottom_up: industry unlevered beta relevered at the company's debt to equity; "
                "little estimation error.",
                "regression: the company's own beta against the S&P 500; use it only when a "
                "regression is reliable.",
                "average: midpoint of bottom-up and regression; only when a regression meets "
                "the R^2 threshold but fails the standard-error threshold.",
                f"The default rule chooses: {rule}. Keep it unless the facts below clearly "
                "support another method.",
                f"Industry {inputs.industry.name}: unlevered beta "
                f"{inputs.industry.values[UNLEVERED_BETA].value:.2f}, market debt to equity "
                f"{de:.2f}, bottom-up beta {bottom_up:.2f}.",
                assessment("Two-year weekly regression", weekly),
                assessment("Five-year monthly regression", monthly),
                "Answer with the method and one short sentence (at most 25 words).",
            ]
        )

    def support(
        self, method: BetaMethod, weekly: Regression | None, monthly: Regression | None
    ) -> tuple[Regression | None, str | None]:
        """(regression to use, None) when the evidence supports the method; else (None, why)."""
        fits = [f for f in (weekly, monthly) if f is not None]
        if method == "bottom_up":
            return None, None
        if method == "regression":
            good = [f for f in fits if reliable(f)]
            if not good:
                return None, "no regression window meets the reliability rule"
            return min(good, key=lambda f: f.standard_error), None
        # Explains the stock but is imprecise: shrink toward the industry beta.
        informative = [f for f in fits if f.r_squared >= MIN_R_SQUARED and not reliable(f)]
        if not informative:
            return None, (
                f"no regression window meets R^2 >= {MIN_R_SQUARED} while failing only the "
                "standard error"
            )
        return min(informative, key=lambda f: f.standard_error), None

    def run(self, bundle: DataBundle) -> tuple[WACCResult, WaccDecision]:
        inputs = assemble(bundle, marginal_tax=self.marginal_tax).wacc_inputs
        weekly, monthly, issues = self.regressions(bundle)
        de = inputs.debt_value.value / inputs.equity_value.value
        tax = inputs.marginal_tax.value  # the filing's rate when read, else the policy
        bottom_up = relever(inputs.industry.values[UNLEVERED_BETA].value, de, tax)
        rule_name, _, rule_note = choose_beta(bottom_up, weekly)
        rule: BetaMethod = "regression" if rule_name == "regression" else "bottom_up"
        evidence = [f"Bottom-up beta {bottom_up:.2f} ({inputs.industry.name})"]
        evidence += [f.describe() for f in (weekly, monthly) if f is not None] + issues
        method, reason, source = rule, rule_note, "rule"
        rejected = None
        used = weekly
        if (weekly or monthly) is not None and self.llm is not None:
            try:
                reply = self.llm.generate(
                    "wacc",
                    self.prompt(inputs, bottom_up, weekly, monthly, rule),
                    BetaChoice,
                    prompt_version=PROMPT_VERSION,
                ).value
            except LLMError as error:
                evidence.append(f"Model unavailable, default rule used: {error}")
            else:
                fit, why = self.support(reply.method, weekly, monthly)
                if why is None:
                    method, reason, source = reply.method, reply.reason.strip(), "model"
                    used = fit if fit is not None else weekly
                else:
                    rejected = f"model chose {reply.method} ({reply.reason.strip()}); {why}"
        other = monthly if used is weekly else weekly
        result = compute_wacc(
            replace(
                inputs,
                regression=used,
                alternative=other,
                beta_method=method,
                method_reason=reason if source == "model" else None,
                rule=(rule, rule_note),
            )
        )
        if rejected is not None:
            result = result.model_copy(
                update={"method_notes": (*result.method_notes, "Rejected: " + rejected)}
            )
        return result, WaccDecision(
            method=method,
            rule_method=rule,
            reason=reason,
            source=source,  # type: ignore[arg-type]
            flagged=method != rule,
            rejected=rejected,
            evidence=tuple(evidence),
        )
