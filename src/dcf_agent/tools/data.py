"""Bounded tools shared by deterministic orchestration and future graph agents."""

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Literal

from dcf_agent.agents.lines import MappingStore
from dcf_agent.agents.snapshot import SnapshotAgent, SnapshotResult
from dcf_agent.contracts.accounting import FilingInsights
from dcf_agent.contracts.external import BenchmarkTable, Dataset, InputReadiness, PriceHistory
from dcf_agent.contracts.inputs import (
    FinancialConcept,
    FinancialPeriod,
    MacroObservation,
    RawFinancials,
    ValuationRequest,
)
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.contracts.snapshot import FinancialSnapshot
from dcf_agent.data.cache import DataUnavailable
from dcf_agent.data.damodaran import DamodaranClient
from dcf_agent.data.extraction import Extraction, extract
from dcf_agent.data.financials import build_raw_financials
from dcf_agent.data.fred import FREDClient, Series
from dcf_agent.data.history import BROADER, CALENDAR_TAGS, build_history
from dcf_agent.data.industry import IndustryMatch
from dcf_agent.data.prices import PriceClient
from dcf_agent.data.scope import assess_scope
from dcf_agent.data.sec import Filing, SECClient, SECError
from dcf_agent.data.statement_values import StatementValue, statement_values
from dcf_agent.data.xbrl import XBRLPackage, read_package
from dcf_agent.tools.edgar import EDGARTools, EvidenceCell
from dcf_agent.tools.snapshot import Section, read_filing_facts, read_statement

if TYPE_CHECKING:  # the filing parser imports this module
    from dcf_agent.rag.index import FilingIndex

ToolName = Literal[
    "get_filing_seed",
    "get_cover_shares",
    "get_industry",
    "get_sec_snapshot",
    "get_raw_financials",
    "get_financial_history",
    "get_price_history",
    "get_fred_rate",
    "get_damodaran",
]
TOOL_DESCRIPTIONS = {
    "get_filing_seed": (
        "Fiscal period, fiscal year and reported values of the latest eligible 10-K, "
        "read from its own XBRL package."
    ),
    "get_cover_shares": (
        "Cover-page common shares outstanding of the latest eligible periodic filing, "
        "with the date they were measured."
    ),
    "get_industry": (
        "Damodaran industry for the company: by ticker, else the most common for its SIC code."
    ),
    "get_financial_history": (
        "Build five annual periods, eight standalone quarters and TTM from eligible SEC facts."
    ),
    "get_raw_financials": (
        "Enrich one SEC period with standard tagged totals and explicit missing concepts."
    ),
    "get_sec_snapshot": (
        "Read a dated company filing; return cited financial fields and validation diagnostics."
    ),
    "get_price_history": (
        "Daily USD price history, corporate actions, and explicit adjustment limitations."
    ),
    "get_fred_rate": (
        "DGS10 Treasury yield, BAMLC0A0CM investment-grade OAS, "
        "or T10YIE breakeven inflation, as decimal rates at a "
        "historical vintage."
    ),
    "get_damodaran": (
        "Dated US industry betas, margins, sales-to-capital, "
        "country risk, or large-nonfinancial-company rating "
        "spreads."
    ),
}


class DataTools:
    """Bind identity and cutoff once; tool callers choose only allowed sources."""

    def __init__(
        self,
        request: ValuationRequest,
        *,
        sec: SECClient,
        snapshot_agent: SnapshotAgent,
        fred: FREDClient,
        damodaran: DamodaranClient,
        prices: PriceClient,
        snapshot_result: SnapshotResult | None = None,
        mappings: MappingStore | None = None,
        filings: "FilingIndex | None" = None,
    ) -> None:
        self.request = request
        # Filing text index for notes and tables; None skips filing insights.
        self.filings = filings
        # Reviewed extension-line decisions; history itself makes no model calls.
        self.mappings = mappings
        self.extractions: tuple[Extraction, ...] = ()
        self.sec, self.snapshot_agent = sec, snapshot_agent
        self.fred, self.damodaran, self.prices = fred, damodaran, prices
        self.audit: list[str] = []
        self.snapshot_result = snapshot_result

    def get_filing_seed(self) -> "FilingSeed":
        self.audit.append("get_filing_seed")
        return filing_seed(self.sec, self.request.cik, self.request.valuation_date, self.mappings)

    def get_cover_shares(self) -> "CoverShares":
        self.audit.append("get_cover_shares")
        return cover_shares(self.sec, self.request.cik, self.request.valuation_date)

    def get_industry(self, betas: BenchmarkTable) -> IndustryMatch:
        self.audit.append("get_industry")
        profile = self.sec.get_company_profile(self.request.cik)
        return self.damodaran.industry_for(self.request.ticker, profile.get("sic"), betas)

    def get_sec_snapshot(
        self,
        *,
        period_start: date,
        period_end: date,
        fiscal_year: int,
        fiscal_quarter: int | None = None,
    ) -> SnapshotResult:
        self.audit.append("get_sec_snapshot")
        if self.snapshot_result is not None:
            saved = self.snapshot_result.snapshot
            if (
                saved.cik,
                saved.as_of,
                saved.period_start,
                saved.period_end,
                saved.fiscal_year,
                saved.fiscal_quarter,
            ) != (
                self.request.cik,
                self.request.valuation_date,
                period_start,
                period_end,
                fiscal_year,
                fiscal_quarter,
            ):
                raise DataUnavailable("Saved SEC interpretation does not match this request")
            self.audit.append("reuse_validated_sec_snapshot")
            return self.snapshot_result
        tools = EDGARTools(
            self.sec, self.request.cik, self.request.valuation_date, period_end=period_end
        )
        return self.snapshot_agent.run(
            tools, period_start=period_start, fiscal_year=fiscal_year, fiscal_quarter=fiscal_quarter
        )

    def get_raw_financials(
        self, snapshot: FinancialSnapshot
    ) -> tuple[RawFinancials, dict[FinancialConcept, str], tuple[str, ...]]:
        self.audit.append("get_raw_financials")
        if snapshot.cik != self.request.cik or snapshot.as_of != self.request.valuation_date:
            raise DataUnavailable("Raw extraction snapshot differs from request")
        tools = EDGARTools(self.sec, snapshot.cik, snapshot.as_of, period_end=snapshot.period_end)
        try:
            return build_raw_financials(snapshot, tools)
        finally:
            self.audit.extend(tools.audit)

    def get_price_history(self, start: date) -> PriceHistory:
        self.audit.append("get_price_history")
        return self.prices.get_price_history(
            self.request.ticker, start, self.request.valuation_date
        )

    def get_financial_history(self, seed: RawFinancials) -> tuple[RawFinancials, dict[str, str]]:
        self.audit.append("get_financial_history")
        if seed.cik != self.request.cik or seed.as_of != self.request.valuation_date:
            raise DataUnavailable("History seed differs from request")
        result, issues, self.extractions = collect_history(
            self.sec, seed, self.mappings, self.audit
        )
        return result, issues

    def get_filing_insights(
        self, raw: RawFinancials | None
    ) -> tuple[FilingInsights, RawFinancials | None, list[str]]:
        """Dilution, lease rate, marginal tax and captive split from the filings (no model)."""
        self.audit.append("get_filing_insights")
        # Imported here: the filing parser uses this module's filing selection.
        from dcf_agent.rag.filings import filing_texts
        from dcf_agent.rag.insights import load, read_insights
        from dcf_agent.rag.releases import guidance_items

        if self.filings is None:
            raise DataUnavailable("No filing index configured")
        cik, as_of = self.request.cik, self.request.valuation_date
        texts = filing_texts(self.sec, cik, as_of)
        for text in texts:
            self.filings.add(text, cik)
        filings = load(self.filings, [t.accession for t in texts])
        insights, raw, notes = read_insights(filings, cik, as_of, raw)
        # Guidance from the latest earnings releases (8-K Item 2.02, Exhibit 99.1).
        try:
            guidance, _, why = guidance_items(self.sec, cik, as_of)
        except SECError as error:
            guidance, why = [], [f"earnings releases not read: {error}"]
        insights = insights.model_copy(update={"management_guidance": tuple(guidance)})
        return insights, raw, [*notes, *why]

    def get_scope(self) -> InputReadiness:
        """v1 scope from SEC metadata and the latest filing's own XBRL."""
        self.audit.append("get_scope")
        latest = max(
            self.extractions, key=lambda e: e.package.filing.report_date or date.min, default=None
        )
        return assess_scope(self.sec.get_company_profile(self.request.cik), latest)

    def get_fred_rate(self, series: Series) -> MacroObservation:
        self.audit.append("get_fred_rate:" + series)
        return self.fred.get_rate(series, self.request.valuation_date)

    def get_damodaran(self, dataset: Dataset) -> BenchmarkTable:
        self.audit.append("get_damodaran:" + dataset)
        return self.damodaran.get_dataset(dataset, self.request.valuation_date)


@dataclass(frozen=True)
class FilingSeed:
    """The latest annual report's own period and reported values."""

    seed: RawFinancials
    filing: Filing
    period: FinancialPeriod
    source_cik: str


@dataclass(frozen=True)
class CoverShares:
    shares: Sourced[float]
    observed_on: date
    filing: Filing


def periodic_filings(
    sec: SECClient, cik: str, as_of: date, forms: tuple[str, ...]
) -> tuple[str, list[Filing]]:
    """Eligible filings of the registrant that actually reports them, newest first."""
    try:
        source = reporting_entity(sec, cik, as_of) or cik
    except SECError:
        source = cik
    filings = sorted(
        (
            f
            for f in sec.get_submissions(source, as_of)
            if f.form in forms
            and f.report_date is not None
            and f.report_date <= as_of
            and f.filed <= as_of
        ),
        key=lambda f: (f.report_date or date.min, f.filed),
        reverse=True,
    )
    return source, filings


def filing_seed(
    sec: SECClient, cik: str, as_of: date, mappings: MappingStore | None = None
) -> FilingSeed:
    """Seed history from the latest 10-K: its full-year period and reported values.

    The fiscal year start and end come from the filing's own full-year facts, so no
    calendar input is needed; the fiscal year is the filing's document focus.
    """
    source, filings = periodic_filings(sec, cik, as_of, ("10-K",))
    if not filings:
        raise DataUnavailable("No eligible 10-K filed on or before the valuation date")
    filing = filings[0]
    package = read_package(sec, source, filing, as_of)
    extraction = extract(package, as_of, mappings.load(cik) if mappings else {})
    end = filing.report_date
    assert end is not None
    starts = sorted(
        {
            start
            for start, period_end in extraction.periods
            if start is not None and period_end == end and 350 <= (end - start).days + 1 <= 380
        }
    )
    if len(starts) != 1:
        raise DataUnavailable(
            f"10-K {filing.accession} has no unique full-year period ending {end}"
        )
    values: dict[FinancialConcept, Sourced[float]] = {}
    for key in ((starts[0], end), (None, end)):
        for concept, item in extraction.periods.get(key, {}).items():
            # History reads this same package and decides broader-basis lines against
            # exact tags itself; offering them again as seed values would conflict.
            broader = item.basis is not None and BROADER.search(item.basis) is not None
            if item.status == "reported" and item.value is not None and not broader:
                values.setdefault(concept, item.value)
    if "revenue" not in values and "net_income" not in values:
        raise DataUnavailable(f"10-K {filing.accession} reports no full-year income statement")
    period = FinancialPeriod(
        kind="annual",
        fiscal_year=package.fiscal_year,
        period_start=starts[0],
        period_end=end,
        values=values,
    )
    seed = RawFinancials(cik=cik, as_of=as_of, annual=(period,))
    return FilingSeed(seed=seed, filing=filing, period=period, source_cik=source)


SHARES = "dei:EntityCommonStockSharesOutstanding"
CLASS_AXIS = "us-gaap:StatementClassOfStockAxis"


def package_cover_shares(package: XBRLPackage, cik: str, as_of: date) -> CoverShares | None:
    """Cover-page shares: the total, else the sum over share classes at one date."""
    totals = [f for f in package.facts if f"{f.namespace}:{f.tag}" == SHARES and f.start is None]
    if totals:
        fact = max(totals, key=lambda f: f.end)
        return CoverShares(fact.sourced(cik, as_of), fact.end, package.filing)
    by_date: dict[date, list[tuple[str, float]]] = {}
    for (concept, start, end, unit, members), amount in package.dimensional.items():
        if concept == SHARES and start is None and unit == "shares":
            if len(members) == 1 and members[0][0] == CLASS_AXIS:
                by_date.setdefault(end, []).append((members[0][1], amount))
    if not by_date:
        return None
    end = max(by_date)
    classes = sorted(by_date[end])
    filing = package.filing
    ref = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{filing.accession.replace('-', '')}/{filing.primary_document}"
    )
    return CoverShares(
        Sourced[float](
            value=sum(amount for _, amount in classes),
            prov=Provenance(
                source="sec_xbrl",
                ref=ref,
                as_of=as_of,
                period_end=end,
                filed=filing.filed,
                confidence="high",
                note=f"{SHARES} summed over {len(classes)} share classes: "
                + ", ".join(f"{member} {amount:,.0f}" for member, amount in classes),
            ),
        ),
        end,
        filing,
    )


def cover_shares(sec: SECClient, cik: str, as_of: date) -> CoverShares:
    """Shares from the newest periodic filing that reports them on its cover page."""
    source, filings = periodic_filings(sec, cik, as_of, ("10-K", "10-Q", "10-K/A", "10-Q/A"))
    for filing in filings[:3]:
        try:
            found = package_cover_shares(read_package(sec, source, filing, as_of), source, as_of)
        except SECError:
            continue
        if found is not None and found.observed_on <= as_of and found.shares.value > 0:
            return found
    raise DataUnavailable("No cover-page shares in the three latest periodic filings")


ENTITY = re.compile(r"Instance reports entity ([0-9]{10}), not the requested company")


def reporting_entity(sec: SECClient, cik: str, as_of: date) -> str | None:
    """The CIK a company's latest periodic filings actually report, when it differs.

    After a holding-company reorganization the new registrant may list filings whose
    XBRL entity (and earlier history) is the predecessor's.
    """
    latest = sorted(
        (
            f
            for f in sec.get_submissions(cik, as_of)
            if f.form in ("10-K", "10-Q") and f.report_date is not None
        ),
        key=lambda f: (f.report_date or date.min, f.filed),
        reverse=True,
    )[:2]
    for filing in latest:
        try:
            read_package(sec, cik, filing, as_of)
            return None
        except SECError as error:
            match = ENTITY.search(str(error))
            if match:
                return match.group(1)
    return None


def collect_history(
    sec: SECClient, seed: RawFinancials, mappings: MappingStore | None, audit: list[str]
) -> tuple[RawFinancials, dict[str, str], tuple[Extraction, ...]]:
    """Dated SEC history: each filing's XBRL package first, companyfacts tag rules second."""
    fallback_issues: dict[str, str] = {}
    source = seed.cik
    try:
        predecessor = reporting_entity(sec, seed.cik, seed.as_of)
    except SECError:
        predecessor = None
    if predecessor is not None:
        source = predecessor
        fallback_issues["successor"] = (
            f"Periodic filings report entity {predecessor}; history read from that registrant"
        )
    facts = list(sec.get_company_facts(source, seed.as_of))
    filings = sec.get_submissions(source, seed.as_of)
    recent = sorted(
        (
            f
            for f in filings
            if f.form in ("10-K", "10-Q", "10-K/A", "10-Q/A")
            and f.report_date is not None
            and f.report_date <= seed.as_of
            and f.filed <= seed.as_of
        ),
        key=lambda f: (f.report_date or date.min, f.filed),
        reverse=True,
    )[:28]
    missing = [
        f
        for f in recent
        if not any(
            x.accession == f.accession and x.end == f.report_date and x.tag in CALENDAR_TAGS
            for x in facts
        )
    ]
    selected = {f.accession: f for f in missing[:8]}
    for form, limit in (("10-K", 5), ("10-Q", 9)):
        endpoints = set()
        for filing in recent:
            if filing.form.removesuffix("/A") != form or filing.report_date in endpoints:
                continue
            endpoints.add(filing.report_date)
            selected[filing.accession] = filing
            if len(endpoints) == limit:
                break
    evidence: list[StatementValue] = []
    extractions: list[Extraction] = []
    learned = mappings.load(seed.cik) if mappings else {}
    absent = {f.accession for f in missing[:8]}
    for filing in selected.values():
        audit.append("read_history_filing:" + filing.accession)
        packaged = False
        try:
            # The filing's own XBRL package: statements, extensions and calculations.
            package = read_package(sec, source, filing, seed.as_of)
            extractions.append(extract(package, seed.as_of, learned))
            audit.append("read_xbrl_package:" + filing.accession)
            packaged = True
            if filing.accession in absent:
                facts.extend(f for f in package.facts if f.namespace == "us-gaap")
        except SECError as error:
            fallback_issues["xbrl_package:" + filing.accession] = str(error)
        if packaged:
            continue
        try:
            tools = EDGARTools(sec, source, seed.as_of, period_end=filing.report_date)
            tools.filing = filing  # Pin the exact missing accession, including amendments.
            tools.reconciliation_facts = tuple(facts)
            statements: dict[Section, tuple[EvidenceCell, ...]] = {}
            for section in ("balance_sheet", "income_statement", "cash_flow", "disclosures"):
                statements[section] = read_statement(tools, section)
            values, recipe_issues = statement_values(tools, statements)
            evidence.extend(values)
            fallback_issues.update(recipe_issues)
            facts.extend(read_filing_facts(tools, statements))
            audit.extend(tools.audit)
        except SECError as error:
            fallback_issues["filing_fallback:" + filing.accession] = str(error)
    if len(missing) > 8:
        fallback_issues["filing_fallback_budget"] = (
            "At most eight missing filings parsed per collection"
        )
    result, issues = build_history(
        seed,
        tuple(facts),
        filings,
        statement_evidence=tuple(evidence),
        extractions=tuple(extractions),
        source_cik=source,
    )
    return result, {**fallback_issues, **issues}, tuple(extractions)
