"""Rule-based normalization (v0): reported history to economic metrics.

v1 treatments: operating leases are debt (EBIT gains the lease interest
component, invested capital gains the lease liability); stock-based compensation
stays an expense; R&D is not capitalized. Invested capital is measured from the
financing side. Approved adjustments are applied through an explicit hook.
Missing inputs are reported, never replaced by zero; an evidenced absence
(``not_presented``) counts as zero.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from dcf_agent.contracts.accounting import Adjustment, NormalizedFinancials, NormalizedPeriod
from dcf_agent.contracts.inputs import (
    FinancialConcept,
    FinancialPeriod,
    RawFinancials,
    within_other_lines,
)
from dcf_agent.contracts.provenance import Provenance, Sourced

US_MARGINAL_TAX = 0.25  # 21% federal plus a typical state rate; override per company.
TAX_BOUNDS = (0.0, 0.5)  # Effective rates outside this range fall back to marginal.


class NormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    marginal_tax_rate: float = US_MARGINAL_TAX
    lease_rate: float = 0.05  # Pre-tax cost of debt used for the lease interest component.
    leases_as_debt: bool = True
    # Where the marginal rate comes from (the filing's rate reconciliation, or the policy).
    marginal_tax_note: str = "policy marginal tax rate"


def amount(period: FinancialPeriod, concept: FinancialConcept) -> float | None:
    """A value, an evidenced zero, or None when genuinely unknown."""
    if concept in period.values:
        return period.values[concept].value
    status = period.statuses.get(concept)
    if status is not None and status.status == "not_presented":
        return 0.0
    return None


def require(period: FinancialPeriod, concepts: Sequence[FinancialConcept]) -> dict[str, float]:
    found = {c: amount(period, c) for c in concepts}
    missing = [c for c, v in found.items() if v is None]
    if missing:
        raise NormalizationError(
            f"{period.kind} {period.period_end}: missing " + ", ".join(sorted(missing))
        )
    return {c: v for c, v in found.items() if v is not None}


def derived(value: float, period: FinancialPeriod, as_of: date, note: str) -> Sourced[float]:
    refs = sorted({v.prov.ref for v in period.values.values()})[:6]
    return Sourced[float](
        value=value,
        prov=Provenance(
            source="derived",
            ref=" | ".join(refs) or "normalize",
            as_of=as_of,
            period_end=period.period_end,
            confidence="medium",
            note=note,
        ),
    )


def operating_nwc(period: FinancialPeriod) -> float | None:
    """Receivables + inventory - payables (cash and debt are financing items)."""
    receivables = amount(period, "receivables")
    inventory = amount(period, "inventory")
    payables = amount(period, "payables")
    if receivables is None or inventory is None or payables is None:
        return None
    return receivables + inventory - payables


def invested_capital(period: FinancialPeriod, policy: Policy) -> tuple[float, str]:
    """Financing side: equity claims + debt (+ leases) - cash and securities."""
    v = require(
        period,
        (
            "equity",
            "minority_interest",
            "preferred_equity",
            "debt",
            "cash",
        ),
    )
    # Securities that are not a statement line of their own sit inside other lines (cash,
    # other assets): counting them again would double count, so none is deducted then.
    securities = amount(period, "marketable_securities")
    within = within_other_lines(period.statuses.get("marketable_securities"))
    if securities is None and not within:
        require(period, ("marketable_securities",))  # genuinely missing: reported, not zero
    v["marketable_securities"] = securities or 0.0
    total = (
        v["equity"]
        + v["minority_interest"]
        + v["preferred_equity"]
        + (amount(period, "temporary_equity") or 0.0)
        + v["debt"]
        - v["cash"]
        - v["marketable_securities"]
    )
    note = "equity + minority + preferred + temporary equity + debt - cash - securities"
    if securities is None:
        note += " (no separate securities line: none deducted)"
    if policy.leases_as_debt:
        leases = amount(period, "operating_lease_liabilities")
        if leases is not None:
            total += leases
            note += " + operating lease liabilities"
        else:
            note += "; operating leases not disclosed (not capitalized)"
    finance = amount(period, "finance_lease_liabilities")
    debt_status = period.statuses.get("debt")
    if finance and not (debt_status and "finance lease" in (debt_status.basis or "")):
        total += finance
        note += " + finance lease liabilities"
    return total, note


def normalize_period(
    period: FinancialPeriod,
    previous: FinancialPeriod | None,
    as_of: date,
    policy: Policy,
    adjustments: Sequence[Adjustment] = (),
) -> NormalizedPeriod:
    if period.kind not in ("annual", "ttm"):
        raise NormalizationError("Only annual and TTM periods are normalized")
    v = require(period, ("revenue", "ebit", "pretax_income", "income_tax_expense"))
    if "revenue" not in period.values or v["revenue"] <= 0:
        raise NormalizationError(
            f"{period.kind} {period.period_end}: no revenue reported (a pre-revenue company); "
            "valuing operating cash flows needs revenue, so it is outside v1 "
            "(pre-revenue and early-stage methods are Phase 7)"
        )
    ebit = v["ebit"]
    ebit_note = "reported operating income"
    leases = amount(period, "operating_lease_liabilities")
    if policy.leases_as_debt and leases:
        lease_interest = leases * policy.lease_rate
        ebit += lease_interest
        ebit_note += f" + lease interest component ({leases:,.0f} x {policy.lease_rate:.2%})"
    applied = [a for a in adjustments if a.period_end == period.period_end]
    ebit_adjust = sum(
        (a.amount.value if a.direction == "increase" else -a.amount.value)
        for a in applied
        if a.target == "ebit"
    )
    if ebit_adjust:
        ebit += ebit_adjust
        ebit_note += f" {ebit_adjust:+,.0f} approved adjustments"
    pretax = v["pretax_income"]
    effective = v["income_tax_expense"] / pretax if pretax > 0 else None
    if effective is not None and TAX_BOUNDS[0] <= effective <= TAX_BOUNDS[1]:
        tax = effective
        tax_note = "effective tax rate (income tax / pretax income)"
    else:
        tax = policy.marginal_tax_rate
        tax_note = "marginal rate: effective rate undefined or outside 0-50%"
    capital, capital_note = invested_capital(period, policy)
    capital += sum(
        (a.amount.value if a.direction == "increase" else -a.amount.value)
        for a in applied
        if a.target == "invested_capital"
    )
    nopat = ebit * (1 - tax)
    average = capital
    if previous is not None:
        try:
            average = (capital + invested_capital(previous, policy)[0]) / 2
        except NormalizationError:
            pass
    nwc = operating_nwc(period)
    da = amount(period, "depreciation_amortization")
    capex = amount(period, "capex")
    reinvestment = None
    if capex is not None and da is not None:
        prior_nwc = operating_nwc(previous) if previous is not None else None
        change = nwc - prior_nwc if nwc is not None and prior_nwc is not None else 0.0
        reinvestment = capex - da + change

    def s(value: float, note: str) -> Sourced[float]:
        return derived(value, period, as_of, note)

    return NormalizedPeriod(
        kind="ttm" if period.kind == "ttm" else "annual",
        period_start=period.period_start,
        period_end=period.period_end,
        revenue=period.values["revenue"],
        clean_ebit=s(ebit, ebit_note),
        nopat=s(nopat, f"clean EBIT x (1 - {tax:.2%}); {tax_note}"),
        invested_capital=s(capital, capital_note),
        effective_tax_rate=s(effective if effective is not None else tax, tax_note),
        marginal_tax_rate=s(policy.marginal_tax_rate, policy.marginal_tax_note),
        ebitda=s(ebit + da, "clean EBIT + D&A") if da is not None else None,
        operating_nwc=s(nwc, "receivables + inventory - payables") if nwc is not None else None,
        net_ppe=period.values.get("net_ppe"),
        roic=s(nopat / average, "NOPAT / average invested capital") if average > 0 else None,
        sales_to_capital=(
            s(v["revenue"] / average, "revenue / average invested capital") if average > 0 else None
        ),
        reinvestment=(
            s(reinvestment, "capex - D&A + change in operating NWC")
            if reinvestment is not None
            else None
        ),
        reinvestment_rate=(
            s(reinvestment / nopat, "reinvestment / NOPAT")
            if reinvestment is not None and nopat > 0
            else None
        ),
        adjustment_ids=tuple(a.adjustment_id for a in applied),
    )


ROIC_REVIEW = 1.0


def flag(period: NormalizedPeriod, issues: dict[str, str]) -> None:
    """Economically extreme ratios are kept but flagged before they anchor a forecast."""
    if period.roic is not None and period.roic.value > ROIC_REVIEW:
        issues[f"{period.kind}:{period.period_end}:roic"] = (
            f"ROIC {period.roic.value:.0%}: invested capital is small after netting cash and "
            "securities; review before using it as a forecast anchor"
        )
    if period.invested_capital.value <= 0:
        issues[f"{period.kind}:{period.period_end}:invested_capital"] = (
            "Invested capital is not positive; ROIC and sales-to-capital are undefined"
        )


def normalize(
    raw: RawFinancials, policy: Policy | None = None, adjustments: Sequence[Adjustment] = ()
) -> tuple[NormalizedFinancials, dict[str, str]]:
    """Every annual period and the TTM that can be normalized; the rest are reported."""
    policy = policy or Policy()
    issues: dict[str, str] = {}
    annual: list[NormalizedPeriod] = []
    previous: FinancialPeriod | None = None
    for period in sorted(raw.annual, key=lambda p: p.period_end):
        try:
            annual.append(normalize_period(period, previous, raw.as_of, policy, adjustments))
            flag(annual[-1], issues)
        except NormalizationError as error:
            issues[f"annual:{period.period_end}"] = str(error)
        previous = period
    ttm = None
    if raw.ttm is not None:
        prior = max(
            (p for p in raw.annual if p.period_end < raw.ttm.period_end),
            key=lambda p: p.period_end,
            default=None,
        )
        try:
            ttm = normalize_period(raw.ttm, prior, raw.as_of, policy, adjustments)
            flag(ttm, issues)
        except NormalizationError as error:
            issues["ttm"] = str(error)
    if not annual and ttm is None:
        raise NormalizationError("No period could be normalized: " + "; ".join(issues.values()))
    return (
        NormalizedFinancials(
            cik=raw.cik,
            as_of=raw.as_of,
            annual=tuple(annual),
            ttm=ttm,
            leases_as_debt=policy.leases_as_debt,
        ),
        issues,
    )
