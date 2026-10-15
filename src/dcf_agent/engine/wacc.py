"""Cost of capital calculators: beta, synthetic rating, weights and WACC.

Formulas (docs/02-agents.md, WACC Agent):
- Ke = Rf + beta x ERP + CRP
- levered beta = unlevered beta x (1 + (1 - t) x D/E)
- Kd = Rf + default spread (synthetic rating from interest coverage)
- WACC = E/(D+E) x Ke + D/(D+E) x Kd x (1 - t)
Default beta rule: bottom-up, unless the regression has R^2 >= 0.3 and a
standard error <= 0.15. The WACC Agent may override the method (bottom-up,
regression or their average) with a reason; the override and the rule's own
choice are both recorded. Pure functions; data retrieval happens elsewhere.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import product
from math import sqrt
from typing import Literal

from dcf_agent.contracts.external import BenchmarkRow, BenchmarkTable
from dcf_agent.contracts.modeling import WACCResult
from dcf_agent.contracts.provenance import Provenance, Sourced

UNLEVERED_BETA = "Unlevered beta corrected for cash"
MIN_R_SQUARED = 0.3
MAX_STANDARD_ERROR = 0.15
BAND_SHIFT = 0.005
BETA_SHIFT = 0.1


class WaccError(ValueError):
    pass


BetaMethod = Literal["bottom_up", "regression", "average"]
Frequency = Literal["weekly", "monthly"]
MINIMUM_OBSERVATIONS: dict[str, int] = {"weekly": 52, "monthly": 36}


@dataclass(frozen=True)
class Regression:
    beta: float
    r_squared: float
    standard_error: float
    observations: int
    frequency: Frequency = "weekly"

    def describe(self) -> str:
        unit = "weeks" if self.frequency == "weekly" else "months"
        return (
            f"{self.frequency} OLS, {self.observations} {unit}, beta {self.beta:.2f}, "
            f"R^2 {self.r_squared:.2f}, SE {self.standard_error:.2f}"
        )


def weekly_returns(prices: Sequence[tuple[date, float]]) -> dict[tuple[int, int], float]:
    """Week-over-week returns on each ISO week's last close."""
    closes: dict[tuple[int, int], tuple[date, float]] = {}
    for day, price in sorted(prices):
        iso = day.isocalendar()
        closes[(iso.year, iso.week)] = (day, price)
    weeks = sorted(closes)
    return {
        week: closes[week][1] / closes[previous][1] - 1
        for previous, week in zip(weeks[:-1], weeks[1:], strict=True)
    }


def monthly_returns(prices: Sequence[tuple[date, float]]) -> dict[tuple[int, int], float]:
    """Month-over-month returns on each calendar month's last close."""
    closes: dict[tuple[int, int], float] = {}
    for day, price in sorted(prices):
        closes[(day.year, day.month)] = price
    months = sorted(closes)
    return {
        month: closes[month] / closes[previous] - 1
        for previous, month in zip(months[:-1], months[1:], strict=True)
    }


def regression_beta(
    stock: Sequence[tuple[date, float]],
    market: Sequence[tuple[date, float]],
    *,
    frequency: Frequency = "weekly",
    minimum: int | None = None,
) -> Regression:
    """OLS slope of the stock's returns on the market's, weekly or monthly."""
    returns = weekly_returns if frequency == "weekly" else monthly_returns
    s, m = returns(stock), returns(market)
    weeks = sorted(set(s) & set(m))
    minimum = minimum or MINIMUM_OBSERVATIONS[frequency]
    if len(weeks) < minimum:
        unit = "weeks" if frequency == "weekly" else "months"
        raise WaccError(f"Regression needs at least {minimum} common {unit}; found {len(weeks)}")
    x = [m[w] for w in weeks]
    y = [s[w] for w in weeks]
    n = len(weeks)
    mean_x, mean_y = sum(x) / n, sum(y) / n
    sxx = sum((a - mean_x) ** 2 for a in x)
    if sxx == 0:
        raise WaccError("Market returns have no variance")
    sxy = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y, strict=True))
    beta = sxy / sxx
    alpha = mean_y - beta * mean_x
    residual = sum((b - alpha - beta * a) ** 2 for a, b in zip(x, y, strict=True))
    total = sum((b - mean_y) ** 2 for b in y)
    return Regression(
        beta=beta,
        r_squared=1 - residual / total if total else 0.0,
        standard_error=sqrt(residual / (n - 2) / sxx),
        observations=n,
        frequency=frequency,
    )


def relever(unlevered: float, debt_to_equity: float, tax: float) -> float:
    return unlevered * (1 + (1 - tax) * debt_to_equity)


def choose_beta(bottom_up: float, regression: Regression | None) -> tuple[str, float, str]:
    if (
        regression is not None
        and regression.r_squared >= MIN_R_SQUARED
        and regression.standard_error <= MAX_STANDARD_ERROR
    ):
        return (
            "regression",
            regression.beta,
            f"Regression beta used: R^2 {regression.r_squared:.2f}, "
            f"standard error {regression.standard_error:.2f}",
        )
    reason = (
        "no regression available"
        if regression is None
        else f"regression R^2 {regression.r_squared:.2f}, standard error "
        f"{regression.standard_error:.2f} below the reliability rule"
    )
    return "bottom_up", bottom_up, f"Bottom-up beta used: {reason}"


def synthetic_rating(ebit: float, interest: float, spreads: BenchmarkTable) -> BenchmarkRow:
    """Interest coverage mapped to Damodaran's rating/spread table."""
    coverage = ebit / interest if interest > 0 else float("inf")
    for row in spreads.rows:
        low, high = row.values["coverage_min"].value, row.values["coverage_max"].value
        if low <= coverage <= high or (coverage == float("inf") and high >= 1e5):
            return row
    best = max(spreads.rows, key=lambda r: r.values["coverage_max"].value)
    if coverage > best.values["coverage_max"].value:
        return best
    raise WaccError(f"No rating bracket for interest coverage {coverage:.2f}")


@dataclass(frozen=True)
class WaccInputs:
    as_of: date
    risk_free: Sourced[float]
    erp: Sourced[float]
    crp: Sourced[float]
    industry: BenchmarkRow
    equity_value: Sourced[float]  # market capitalization
    debt_value: Sourced[float]  # book debt (+ leases when capitalized) as a market proxy
    marginal_tax: Sourced[float]
    spreads: BenchmarkTable
    ebit: float | None = None
    interest_expense: float | None = None
    fallback_spread: Sourced[float] | None = None  # e.g. investment-grade OAS
    regression: Regression | None = None  # the one the rule and the methods use
    alternative: Regression | None = None  # another window, recorded as evidence
    beta_method: BetaMethod | None = None  # None: the default rule decides
    method_reason: str | None = None
    # The rule's verdict when it was taken on another window than `regression`.
    rule: tuple[BetaMethod, str] | None = None


def wacc_of(rf: float, erp: float, crp: float, beta: float, kd_after: float, we: float) -> float:
    return we * (rf + beta * erp + crp) + (1 - we) * kd_after


def compute_wacc(i: WaccInputs) -> WACCResult:
    equity, debt = i.equity_value.value, i.debt_value.value
    if equity <= 0 or debt < 0:
        raise WaccError("Market equity must be positive and debt non-negative")
    tax = i.marginal_tax.value
    de = debt / equity
    unlevered = i.industry.values[UNLEVERED_BETA].value
    bottom_up = relever(unlevered, de, tax)
    method, beta, beta_note = choose_beta(bottom_up, i.regression)
    if i.rule is not None:
        method, beta_note = i.rule
    rf, erp, crp = i.risk_free.value, i.erp.value, i.crp.value
    notes = [beta_note]
    if i.alternative is not None:
        notes.append(f"Second regression window: {i.alternative.describe()}")
    if i.beta_method is not None:
        if i.beta_method != "bottom_up" and i.regression is None:
            raise WaccError(f"Beta method {i.beta_method} needs a regression beta")
        rule = method
        method = i.beta_method
        assert i.regression is not None or method == "bottom_up"
        beta = {
            "bottom_up": bottom_up,
            "regression": i.regression.beta if i.regression else bottom_up,
            "average": (bottom_up + i.regression.beta) / 2 if i.regression else bottom_up,
        }[method]
        notes.append(
            f"Beta method {method} chosen by the WACC Agent"
            + (f": {i.method_reason}" if i.method_reason else "")
        )
        if method != rule:
            notes.append(f"Review: the default rule would use {rule}")
    if i.ebit is not None and i.interest_expense is not None and i.interest_expense > 0:
        row = synthetic_rating(i.ebit, i.interest_expense, i.spreads)
        spread = row.values["spread"].value
        coverage = i.ebit / i.interest_expense
        spread_ref = row.values["spread"].prov.ref
        notes.append(f"Synthetic rating {row.name} from interest coverage {coverage:.2f}")
    elif debt == 0:
        row = synthetic_rating(1.0, 0.0, i.spreads)
        spread, spread_ref = row.values["spread"].value, row.values["spread"].prov.ref
        notes.append("No debt: lowest default spread; debt weight is zero")
    elif i.fallback_spread is not None:
        spread, spread_ref = i.fallback_spread.value, i.fallback_spread.prov.ref
        notes.append("Interest expense not available: market credit spread used, no coverage test")
    else:
        raise WaccError("No interest coverage and no fallback spread for the cost of debt")
    pretax_kd = rf + spread
    aftertax_kd = pretax_kd * (1 - tax)
    we = equity / (equity + debt)
    cost_equity = rf + beta * erp + crp
    wacc = we * cost_equity + (1 - we) * aftertax_kd
    terminal_beta = 1.0 if beta > 1 else beta
    terminal = wacc_of(rf, erp, crp, terminal_beta, aftertax_kd, we)
    if terminal_beta != beta:
        notes.append("Terminal WACC: beta converges to 1 for a mature firm")
    shifted = [
        wacc_of(rf + a, erp + b, crp, max(beta + c, 0.0), (rf + a + spread) * (1 - tax), we)
        for a, b, c in product(
            (-BAND_SHIFT, 0, BAND_SHIFT), (-BAND_SHIFT, 0, BAND_SHIFT), (-BETA_SHIFT, 0, BETA_SHIFT)
        )
    ]
    notes.append(
        f"Band: Rf and ERP +/-{BAND_SHIFT:.1%}, beta +/-{BETA_SHIFT}; D/E {de:.3f} at market value"
    )

    def s(value: float, note: str, ref: str = "dcf_agent.engine.wacc") -> Sourced[float]:
        return Sourced[float](
            value=value,
            prov=Provenance(
                source="derived", ref=ref, as_of=i.as_of, confidence="medium", note=note
            ),
        )

    return WACCResult(
        as_of=i.as_of,
        rf=i.risk_free,
        erp=i.erp,
        crp=i.crp,
        unlevered_beta=i.industry.values[UNLEVERED_BETA],
        relevered_beta=s(bottom_up, f"unlevered {unlevered:.3f} x (1 + (1 - t) x D/E {de:.3f})"),
        bottom_up_beta=s(bottom_up, f"industry {i.industry.name}, relevered at market D/E"),
        regression_beta=(
            s(
                i.regression.beta,
                i.regression.describe(),
            )
            if i.regression is not None
            else None
        ),
        beta_method=method,  # type: ignore[arg-type]
        cost_equity=s(cost_equity, f"Rf + beta {beta:.3f} x ERP + CRP"),
        pretax_kd=s(pretax_kd, f"Rf + default spread {spread:.4f}", spread_ref),
        tax_rate=i.marginal_tax,
        aftertax_kd=s(aftertax_kd, "pre-tax Kd x (1 - marginal tax)"),
        weight_equity=s(we, "market equity / (market equity + debt)"),
        weight_debt=s(1 - we, "debt / (market equity + debt)"),
        wacc=s(wacc, "E/V x Ke + D/V x Kd after tax"),
        terminal_wacc=s(terminal, f"terminal beta {terminal_beta:.3f}"),
        wacc_band=(min(shifted + [wacc]), max(shifted + [wacc])),
        method_notes=tuple(notes),
    )
