"""Deterministic Data Agent: collect every source, preserve partial results.

The latest 10-K's own XBRL package supplies the fiscal period and seed values, the
newest periodic filing's cover page the share count, and Damodaran's company file
the industry. The model-based SEC snapshot is an optional cross-check, not a source.
"""

import math
from datetime import date

from pydantic import ValidationError

from dcf_agent.build import build_fingerprint
from dcf_agent.contracts.external import (
    BenchmarkTable,
    DataBundle,
    IndustrySelection,
    InputReadiness,
    PriceHistory,
)
from dcf_agent.contracts.inputs import (
    FinancialConcept,
    FinancialPeriod,
    MacroData,
    MacroObservation,
    MarketData,
)
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.contracts.quality import DataFallback, DataQualityCheck, DataQualityReport
from dcf_agent.contracts.snapshot import FinancialSnapshot, SnapshotConcept
from dcf_agent.data.cache import DataUnavailable
from dcf_agent.data.damodaran import FILES, DamodaranClient
from dcf_agent.data.fred import SERIES
from dcf_agent.data.readiness import assess_readiness
from dcf_agent.data.sec import SECError
from dcf_agent.llm import LLMError
from dcf_agent.tools.data import DataTools, FilingSeed


def market_from_sources(
    shares: Sourced[float],
    observed_on: date,
    prices: PriceHistory,
    industry: str | None,
    sic: str | None = None,
) -> MarketData:
    """Close price times SEC cover-page shares, aligned for later splits."""
    close = prices.observations[-1]
    if prices.source != "yfinance" or not prices.start <= observed_on <= close.observed_on:
        raise DataUnavailable(
            "Cannot align the SEC share observation with the price and split history"
        )
    factor = math.prod(
        s.ratio for s in prices.splits if observed_on < s.observed_on <= close.observed_on
    )
    count = shares.value * factor
    prov = Provenance(
        source="derived",
        ref=shares.prov.ref + " | " + close.price.prov.ref,
        as_of=prices.as_of,
        period_end=close.observed_on,
        confidence="medium",
        note=(
            f"SEC shares carried forward; split factor {factor:g}. "
            "Buybacks/issuance since observation are not estimated. "
            "Not an independent market-cap cross-check."
        ),
    )
    return MarketData(
        ticker=prices.ticker,
        as_of=prices.as_of,
        close=close,
        shares_outstanding=Sourced[float](value=count, prov=prov),
        shares_observed_on=observed_on,
        market_cap=Sourced[float](value=close.price.value * count, prov=prov),
        price_history=prices.observations,
        history_basis="split_adjusted",
        sic=sic if sic and len(sic) == 4 and sic.isdigit() else None,
        industry=industry,
    )


# Snapshot field -> seed concept, for the optional cross-check.
CROSS_CHECK: dict[SnapshotConcept, FinancialConcept] = {
    "revenue": "revenue",
    "operating_income": "ebit",
    "net_income": "net_income",
    "cash": "cash",
    "debt": "debt",
    "total_assets": "total_assets",
    "total_liabilities": "total_liabilities",
    "equity": "equity",
    "operating_cash_flow": "operating_cash_flow",
    "capex": "capex",
}


def cross_check(snapshot: FinancialSnapshot, period: FinancialPeriod) -> list[str]:
    """Differences between the model reading of the filing and the package extraction."""
    if (snapshot.period_start, snapshot.period_end) != (period.period_start, period.period_end):
        return [
            f"Snapshot period {snapshot.period_start} to {snapshot.period_end} differs from "
            f"the 10-K period {period.period_start} to {period.period_end}"
        ]
    found = []
    for field, concept in CROSS_CHECK.items():
        amount = snapshot.fields[field].amount
        value = period.values.get(concept)
        if amount is None or value is None:
            continue
        if abs(amount.value - value.value) > max(1.0, 0.005 * abs(value.value)):
            found.append(f"{field}: snapshot {amount.value:,.0f}, package {value.value:,.0f}")
    return found


def premiums(table: BenchmarkTable, country: str) -> tuple[MacroObservation, MacroObservation]:
    row = DamodaranClient.select(table, country)
    try:
        total = row.values["Equity Risk Premium"]
        crp = row.values["Country Risk Premium"]
    except KeyError:
        raise DataUnavailable("Damodaran country row lacks required risk premiums") from None
    # The country table's total already includes CRP: subtract before handing the
    # mature-market ERP and CRP separately to downstream WACC calculations.
    observed = table.analysis_on or table.available_on
    erp = MacroObservation(
        observed_on=observed,
        available_on=table.available_on,
        rate=Sourced[float](
            value=total.value - crp.value,
            prov=Provenance(
                source="derived",
                ref=table.ref,
                as_of=table.as_of,
                period_end=observed,
                confidence="medium",
                note=(
                    "Mature-market implied ERP = country total equity risk "
                    "premium minus country risk premium; do not add CRP to "
                    "the total again."
                ),
            ),
        ),
    )
    return erp, MacroObservation(observed_on=observed, available_on=table.available_on, rate=crp)


class DataAgent:
    """The normal fetch path makes no model calls; SEC interpretation is opt-in."""

    def run(
        self,
        tools: DataTools,
        *,
        price_start: date,
        industry: str | None = None,
        country: str = "United States",
        interpret: bool = False,
    ) -> DataBundle:
        """Collect every source for the request.

        industry: an exact Damodaran label; None maps the company automatically.
        interpret: also run the model-based SEC snapshot as a cross-check (a saved
        snapshot given to the tools is always checked).
        """
        as_of = tools.request.valuation_date
        checks: list[DataQualityCheck] = []
        missing: list[str] = []
        fallbacks: list[DataFallback] = []

        def record(field: str, ok: bool, message: str, *, warning: bool = False) -> None:
            checks.append(
                DataQualityCheck(
                    check_id=f"{field}:{len(checks)}",
                    field=field,
                    status="passed" if ok else "failed",
                    severity="warning" if warning else "info" if ok else "error",
                    message=message,
                )
            )
            if not ok and not warning and field not in missing:
                missing.append(field)

        seed: FilingSeed | None = None
        try:
            seed = tools.get_filing_seed()
            record(
                "seed",
                True,
                f"FY{seed.period.fiscal_year} {seed.period.period_start} to "
                f"{seed.period.period_end} from 10-K {seed.filing.accession}; "
                f"{len(seed.period.values)} reported values",
            )
        except (SECError, DataUnavailable, ValidationError) as error:
            record("seed", False, str(error))
        financials = None
        if seed is not None and (interpret or tools.snapshot_result is not None):
            try:
                result = tools.get_sec_snapshot(
                    period_start=seed.period.period_start,
                    period_end=seed.period.period_end,
                    fiscal_year=seed.period.fiscal_year,
                )
                financials = result.snapshot
                differences = cross_check(financials, seed.period)
                record(
                    "financials",
                    not differences,
                    "Model snapshot differs: " + "; ".join(differences)
                    if differences
                    else "Model snapshot agrees with the package extraction",
                    warning=True,
                )
            except (SECError, LLMError, ValidationError, DataUnavailable) as error:
                record("financials", False, "Snapshot cross-check: " + str(error), warning=True)
        raw = seed.seed if seed is not None else None
        raw_issues: dict[FinancialConcept, str] = {}
        history_issues: dict[str, str] = {}
        if seed is not None:
            try:
                raw, history_issues = tools.get_financial_history(seed.seed)
                record(
                    "history",
                    not history_issues,
                    f"{len(raw.annual)} annual periods; {len(raw.quarterly)} quarters; "
                    f"TTM={raw.ttm is not None}; "
                    f"{len(history_issues)} review items in history_issues",
                    warning=True,
                )
            except (SECError, DataUnavailable, ValidationError) as error:
                history_issues["construction"] = str(error)
                record("history", False, str(error))
        insights = None
        if tools.filings is not None:
            try:
                insights, raw, notes = tools.get_filing_insights(raw)
                record(
                    "filings",
                    True,
                    f"{len(insights.dilutive_securities)} dilutive securities, "
                    f"{len(insights.lease_liabilities)} lease rates, "
                    f"{len(insights.tax_items)} marginal tax rates, "
                    f"{len(insights.captive_finance)} captive-finance splits read from the filings",
                )
                for note in notes:
                    record("filings", False, note, warning=True)
            except (SECError, DataUnavailable, ValidationError) as error:
                record("filings", False, "Filing insights: " + str(error), warning=True)
        try:
            scope = tools.get_scope()
        except SECError as error:
            scope = InputReadiness(
                status="blocked",
                missing={"scope": ("Scope could not be checked: " + str(error),)},
                review_notes=(),
            )
        prices = None
        try:
            prices = tools.get_price_history(price_start)
            record(
                "prices",
                True,
                f"{len(prices.observations)} daily observations from {prices.source}",
            )
            for message in prices.warnings:
                record("prices", False, message, warning=True)
            if prices.source == "stooq" or prices.from_cache:
                fallbacks.append(
                    DataFallback(
                        field="prices",
                        attempted_source="yfinance",
                        selected_source="cache:" + prices.source if prices.from_cache else "stooq",
                        reason="; ".join(prices.warnings),
                    )
                )
        except DataUnavailable as error:
            record("prices", False, str(error))
        rates: dict[str, MacroObservation] = {}
        for series in SERIES:
            try:
                rates[series] = tools.get_fred_rate(series)
                record(series, True, "Historical vintage retrieved; percent converted to decimal")
            except DataUnavailable as error:
                record(series, False, str(error))
        benchmarks = {}
        for dataset in FILES:
            try:
                table = tools.get_damodaran(dataset)
                benchmarks[dataset] = table
                record(
                    dataset,
                    True,
                    f"Damodaran version {table.version}; available {table.available_on}",
                )
                for message in table.warnings:
                    record(dataset, False, message, warning=True)
            except DataUnavailable as error:
                record(dataset, False, str(error))
        selection = None
        if industry is not None:
            selection = IndustrySelection(name=industry, method="user", note="Chosen by the user")
        elif "betas" in benchmarks:
            try:
                match = tools.get_industry(benchmarks["betas"])
                selection = IndustrySelection(
                    name=match.industry,
                    method="ticker" if match.method == "ticker" else "sic",
                    sic=match.sic,
                    alternatives=match.alternatives,
                    ref=match.ref,
                    note=match.note,
                )
                record("industry", True, f"{match.industry} by {match.method}: {match.note}")
                if match.method != "ticker":
                    record(
                        "industry",
                        False,
                        "Industry inferred from the SIC code; confirm it. Alternatives: "
                        + (", ".join(match.alternatives) or "none"),
                        warning=True,
                    )
            except (SECError, DataUnavailable) as error:
                record("industry", False, str(error))
        else:
            record("industry", False, "Industry mapping needs the Damodaran betas table")
        for dataset in ("betas", "margins", "sales_to_capital"):
            if dataset not in benchmarks:
                continue
            if selection is None:
                del benchmarks[dataset]
                continue
            try:
                row = DamodaranClient.select(benchmarks[dataset], selection.name)
                # Keep only the selected industry in the agent handoff.
                benchmarks[dataset] = benchmarks[dataset].model_copy(update={"rows": (row,)})
            except DataUnavailable as error:
                del benchmarks[dataset]
                record(dataset, False, str(error))
        macro = None
        try:
            if "country_risk" not in benchmarks or "DGS10" not in rates:
                raise DataUnavailable("Macro assembly needs FRED DGS10 and Damodaran country risk")
            erp, crp = premiums(benchmarks["country_risk"], country)
            macro = MacroData(
                as_of=as_of,
                risk_free_rate=rates["DGS10"],
                equity_risk_premium=erp,
                country_risk_premium=crp,
                credit_spread=rates.get("BAMLC0A0CM"),
                inflation_expectation=rates.get("T10YIE"),
            )
            record(
                "macro",
                True,
                "Treasury yield proxy, mature-market ERP, CRP, and available FRED context",
            )
            record(
                "macro",
                False,
                (
                    "DGS10 is a Treasury yield proxy; sovereign-default "
                    "adjustment and company-specific credit spread selection "
                    "belong to WACC"
                ),
                warning=True,
            )
        except (DataUnavailable, ValidationError) as error:
            record("macro", False, str(error))
        market = None
        try:
            if prices is None:
                raise DataUnavailable("Market assembly needs eligible prices")
            cover = tools.get_cover_shares()
            market = market_from_sources(
                cover.shares,
                cover.observed_on,
                prices,
                selection.name if selection else None,
                selection.sic if selection else None,
            )
            record(
                "market",
                True,
                "Price multiplied by split-aligned SEC cover-page shares "
                f"({cover.filing.form} {cover.filing.accession}, measured {cover.observed_on})",
            )
            record(
                "market",
                False,
                (
                    "Shares are carried forward from their SEC observation; "
                    "market cap is approximate, not independently reconciled"
                ),
                warning=True,
            )
        except (SECError, DataUnavailable, ValidationError) as error:
            record("market", False, str(error))
        return DataBundle(
            request=tools.request,
            financials=financials,
            raw=raw,
            raw_issues=raw_issues,
            history_issues=history_issues,
            readiness={**assess_readiness(raw, history_issues), "scope": scope},
            prices=prices,
            market=market,
            macro=macro,
            rates=rates,
            benchmarks=benchmarks,
            industry=selection,
            filing_insights=insights,
            quality=DataQualityReport(
                as_of=as_of,
                checks=tuple(checks),
                missing_fields=tuple(missing),
                fallbacks=tuple(fallbacks),
            ),
            tool_calls=tuple(tools.audit),
            build=build_fingerprint(),
        )
