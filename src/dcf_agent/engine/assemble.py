"""Assemble engine inputs from a checked DataBundle: WACC inputs, bridge, base and shares.

Only values with an explicit status enter the valuation: reported or
carried-forward amounts, or evidenced absence (zero). Anything else raises with
the concept's reason, so a valuation never silently uses a missing input.
"""

from dataclasses import dataclass, field
from datetime import date

from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import FinancialConcept, FinancialPeriod
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.engine.dcf import Option, Shares
from dcf_agent.engine.wacc import Regression, WaccInputs


class AssemblyError(ValueError):
    pass


@dataclass(frozen=True)
class Assembled:
    as_of: date
    base_period: FinancialPeriod
    base_revenue: float
    wacc_inputs: WaccInputs
    bridge: dict[str, Sourced[float]]
    shares: Shares
    notes: tuple[str, ...] = field(default_factory=tuple)


def latest(bundle: DataBundle) -> FinancialPeriod:
    raw = bundle.raw
    if raw is None:
        raise AssemblyError("No historical financials in the bundle")
    if raw.ttm is not None:
        return raw.ttm
    if raw.annual:
        return raw.annual[-1]
    raise AssemblyError("No TTM or annual period available")


def value(period: FinancialPeriod, concept: FinancialConcept) -> Sourced[float] | None:
    """A sourced amount, an evidenced zero, or an error naming why it is unusable."""
    if concept in period.values:
        return period.values[concept]
    status = period.statuses.get(concept)
    if status is not None and status.status == "not_presented":
        return None
    reason = f"{status.status}: {status.reason}" if status else "no value or status"
    raise AssemblyError(f"{concept} at {period.period_end} is not usable ({reason})")


def negate(item: Sourced[float], note: str) -> Sourced[float]:
    return Sourced[float](
        value=-item.value,
        prov=item.prov.model_copy(update={"note": f"{note}; {item.prov.note or ''}".strip("; ")}),
    )


def build_bridge(
    period: FinancialPeriod, leases_as_debt: bool
) -> tuple[dict[str, Sourced[float]], list[str]]:
    """Signed contributions from enterprise value to equity value."""
    bridge: dict[str, Sourced[float]] = {}
    notes: list[str] = []
    for concept in ("cash", "marketable_securities"):
        item = value(period, concept)
        if item is not None:
            bridge[concept] = item
    claims: list[FinancialConcept] = [
        "debt",
        "minority_interest",
        "preferred_equity",
        "temporary_equity",
    ]
    if leases_as_debt:
        claims.append("operating_lease_liabilities")
    debt_status = period.statuses.get("debt")
    if not (debt_status and "finance lease" in (debt_status.basis or "")):
        claims.append("finance_lease_liabilities")
    for concept in claims:
        try:
            item = value(period, concept)
        except AssemblyError:
            status = period.statuses.get(concept)
            if status is not None and status.status == "not_disclosed" and "lease" in concept:
                notes.append(f"{concept} not disclosed: not deducted")
                continue
            raise
        if item is not None:
            bridge[concept] = negate(item, f"deducted from enterprise value: {concept}")
    captive = period.statuses.get("captive_finance_debt")
    if captive is not None and captive.status == "reported":
        notes.append(
            "Captive finance arm separated: its debt is excluded from the bridge; value the "
            "finance arm separately (e.g. at book equity) before relying on per-share value"
        )
    elif captive is not None and captive.status == "unresolved":
        raise AssemblyError("Captive finance debt is not split: " + (captive.reason or ""))
    return bridge, notes


def marginal_tax_rate(
    bundle: DataBundle, default: float, *, filings: bool = True
) -> tuple[Sourced[float], str]:
    """The filing's marginal tax rate (rate reconciliation) when read, else the policy."""
    items = bundle.filing_insights.tax_items if bundle.filing_insights is not None else ()
    if filings and items and items[0].amount is not None:
        return items[0].amount, f"Marginal tax {items[0].amount.value:.2%}: {items[0].description}"
    as_of = bundle.request.valuation_date
    return Sourced[float](
        value=default,
        prov=Provenance(
            source="user_override",
            ref="policy",
            as_of=as_of,
            confidence="medium",
            note="Marginal tax rate policy",
        ),
    ), f"Marginal tax {default:.2%}: policy" + (
        "" if not filings else " (the filings' rate reconciliation was not read)"
    )


def lease_rate(bundle: DataBundle, fallback: float) -> float:
    """The operating leases' weighted-average discount rate, else the fallback (pre-tax Kd)."""
    insights = bundle.filing_insights
    for lease in insights.lease_liabilities if insights is not None else ():
        if lease.lease_type == "operating" and lease.discount_rate is not None:
            return lease.discount_rate.value
    return fallback


def shares_of(bundle: DataBundle, basic: float) -> tuple[Shares, str]:
    """Cover-page shares plus the dilutive securities read from the filings."""
    insights = bundle.filing_insights
    securities = insights.dilutive_securities if insights is not None else ()
    if not securities:
        return Shares(basic=basic), (
            "Shares: dated cover-page count; no dilutive securities read from the filings"
        )
    units = sum(s.count.value for s in securities if s.kind in ("rsu", "psu"))
    options = tuple(
        Option(count=s.count.value, strike=s.strike_price.value)
        for s in securities
        if s.kind in ("option", "warrant") and s.strike_price is not None
    )
    note = (
        f"Shares: cover-page count plus {units / 1e6:,.2f}M unvested units and "
        f"{sum(o.count for o in options) / 1e6:,.2f}M options (treasury stock method) from "
        "the filings; convertibles are not read yet"
    )
    return Shares(basic=basic, rsus=units, options=options), note


def assemble(
    bundle: DataBundle,
    *,
    marginal_tax: float,
    leases_as_debt: bool = True,
    regression: Regression | None = None,
    filings_tax: bool = True,
) -> Assembled:
    """marginal_tax is the policy rate, used unless the filings' rate is read (and wanted)."""
    blocked = {
        name: r.missing
        for name, r in bundle.readiness.items()
        if name in ("wacc", "equity_bridge", "scope") and r.status == "blocked"
    }
    if blocked:
        raise AssemblyError(f"Readiness blocks valuation: {blocked}")
    if bundle.market is None or bundle.macro is None or "betas" not in bundle.benchmarks:
        raise AssemblyError("Market data, macro data and industry betas are required")
    if "spreads" not in bundle.benchmarks:
        raise AssemblyError("Damodaran rating spreads are required for the cost of debt")
    period = latest(bundle)
    as_of = bundle.request.valuation_date
    bridge, notes = build_bridge(period, leases_as_debt)
    debt_total = -sum(
        v.value
        for k, v in bridge.items()
        if k not in ("cash", "marketable_securities")
        and k in ("debt", "operating_lease_liabilities", "finance_lease_liabilities")
    )
    market = bundle.market
    equity_value = market.market_cap or Sourced[float](
        value=market.close.price.value * market.shares_outstanding.value,
        prov=market.shares_outstanding.prov,
    )
    tax, tax_note = marginal_tax_rate(bundle, marginal_tax, filings=filings_tax)
    notes.append(tax_note)
    ebit = period.values.get("ebit")
    interest = period.values.get("interest_expense")
    industry = bundle.benchmarks["betas"].rows
    if len(industry) != 1:
        raise AssemblyError("Exactly one industry beta row is required")
    inputs = WaccInputs(
        as_of=as_of,
        risk_free=bundle.macro.risk_free_rate.rate,
        erp=bundle.macro.equity_risk_premium.rate,
        crp=bundle.macro.country_risk_premium.rate,
        industry=industry[0],
        equity_value=Sourced[float](value=equity_value.value, prov=equity_value.prov),
        debt_value=Sourced[float](
            value=debt_total,
            prov=Provenance(
                source="derived",
                ref="bridge",
                as_of=as_of,
                confidence="medium",
                note="Book debt and capitalized leases as a market-value proxy",
            ),
        ),
        marginal_tax=tax,
        spreads=bundle.benchmarks["spreads"],
        ebit=ebit.value if ebit else None,
        interest_expense=interest.value if interest else None,
        fallback_spread=bundle.rates["BAMLC0A0CM"].rate if "BAMLC0A0CM" in bundle.rates else None,
        regression=regression,
    )
    revenue = period.values.get("revenue")
    if revenue is None:
        raise AssemblyError("Base revenue is required")
    notes.append(f"Base revenue: {period.kind} ending {period.period_end}")
    shares, shares_note = shares_of(bundle, market.shares_outstanding.value)
    notes.append(shares_note)
    return Assembled(
        as_of=as_of,
        base_period=period,
        base_revenue=revenue.value,
        wacc_inputs=inputs,
        bridge=bridge,
        shares=shares,
        notes=tuple(notes),
    )
