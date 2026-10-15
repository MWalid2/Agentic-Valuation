"""All tools participate in the handoff; unavailable sources stay explicit."""

import json
from datetime import date
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError
from test_external_data import CUTOFF, FIXTURES, START, configuration, prices_body

from dcf_agent.agents.data import DataAgent, cross_check, market_from_sources
from dcf_agent.agents.snapshot import SnapshotAgent, SnapshotResult
from dcf_agent.contracts.external import DataBundle, InputReadiness, SplitObservation
from dcf_agent.contracts.inputs import FinancialPeriod, RawFinancials, ValuationRequest
from dcf_agent.data.cache import DataUnavailable
from dcf_agent.data.damodaran import FILES, DamodaranClient
from dcf_agent.data.fred import SERIES, FREDClient
from dcf_agent.data.industry import IndustryMatch
from dcf_agent.data.prices import PriceClient
from dcf_agent.data.sec import Filing, SECClient
from dcf_agent.tools.data import CoverShares, DataTools, FilingSeed

TEN_K = Filing(
    accession="0000320193-25-000079",
    form="10-K",
    filed=date(2025, 10, 31),
    report_date=date(2025, 9, 27),
    primary_document="aapl-20250927.htm",
)


def snapshot():
    return SnapshotResult.model_validate_json((FIXTURES / "apple-snapshot.json").read_text())


def filing_seed(revenue_shift: float = 0.0) -> FilingSeed:
    """The package seed, built here from the recorded snapshot values."""
    s = snapshot().snapshot
    revenue = s.fields["revenue"].amount
    period = FinancialPeriod(
        kind="annual",
        fiscal_year=s.fiscal_year,
        period_start=s.period_start,
        period_end=s.period_end,
        values={
            "revenue": revenue.model_copy(update={"value": revenue.value + revenue_shift}),
            "net_income": s.fields["net_income"].amount,
        },
    )
    return FilingSeed(
        seed=RawFinancials(cik=s.cik, as_of=s.as_of, annual=(period,)),
        filing=TEN_K,
        period=period,
        source_cik=s.cik,
    )


def cover() -> CoverShares:
    field = snapshot().snapshot.fields["shares_outstanding"]
    assert field.amount is not None and field.observed_on is not None
    return CoverShares(field.amount, field.observed_on, TEN_K)


def request() -> ValuationRequest:
    return ValuationRequest(
        ticker="AAPL",
        cik="0000320193",
        company_name="Apple",
        valuation_date=CUTOFF,
        mode="info",
        question="Collect inputs",
    )


def respond(outage):
    def handler(request):
        if "stlouisfed" in request.url.host:
            if outage == "fred":
                return httpx.Response(401)
            assert request.url.params["series_id"] in SERIES
            return httpx.Response(200, json=json.loads((FIXTURES / "fred-dgs10.json").read_text()))
        if "stooq" in request.url.host:
            return httpx.Response(403)
        filename = request.url.path.rsplit("/", 1)[-1]
        dataset = next((k for k, v in FILES.items() if filename == v + ".html"), "index")
        return httpx.Response(200, text=(FIXTURES / f"{dataset}.html").read_text(encoding="utf-8"))

    return handler


def run_agent(tmp_path, monkeypatch, outage=None, saved=None, seed=None, **options):
    config = configuration(tmp_path)

    def load(*_):
        if outage == "prices":
            raise DataUnavailable("simulated outage")
        return prices_body()

    transport = httpx.MockTransport(respond(outage))
    with (
        FREDClient(config, transport=transport) as fred,
        DamodaranClient(config, transport=transport) as damodaran,
        PriceClient(config, transport=transport, loader=load) as prices,
    ):
        interpreter = Mock(spec=SnapshotAgent)
        tools = DataTools(
            request(),
            sec=Mock(spec=SECClient),
            snapshot_agent=interpreter,
            fred=fred,
            damodaran=damodaran,
            prices=prices,
            snapshot_result=saved,
        )

        # SEC extraction has recorded-package tests; isolate orchestration here.
        def seed_result():
            tools.audit.append("get_filing_seed")
            if outage == "sec":
                raise DataUnavailable("No eligible 10-K")
            return seed or filing_seed()

        def shares_result():
            tools.audit.append("get_cover_shares")
            if outage == "shares":
                raise DataUnavailable("No cover-page shares")
            return cover()

        def industry_result(betas):
            tools.audit.append("get_industry")
            if outage == "industry":
                raise DataUnavailable("No Damodaran industry")
            return IndustryMatch(
                industry="Computers/Peripherals",
                method="ticker",
                ticker="AAPL",
                sic="3571",
                alternatives=(),
                ref="https://example.test/indname.xlsx",
                note="Damodaran classification of Apple Inc.",
            )

        def history_result(seed):
            tools.audit.append("get_financial_history")
            if outage == "history":
                raise DataUnavailable("History unavailable")
            return seed, {"annual_history": "Partial orchestration fixture"}

        monkeypatch.setattr(tools, "get_filing_seed", seed_result)
        monkeypatch.setattr(tools, "get_cover_shares", shares_result)
        monkeypatch.setattr(tools, "get_industry", industry_result)
        monkeypatch.setattr(tools, "get_financial_history", history_result)
        monkeypatch.setattr(
            tools,
            "get_scope",
            lambda: InputReadiness(status="needs_review", review_notes=("Scope fixture",)),
        )
        result = DataAgent().run(tools, price_start=START, **options)
    interpreter.run.assert_not_called()
    return result


@pytest.mark.parametrize("outage", [None, "fred", "prices", "sec", "industry", "shares", "history"])
def test_data_agent_calls_all_sources_and_preserves_partial_results(tmp_path, outage, monkeypatch):
    result = run_agent(tmp_path, monkeypatch, outage)
    assert result.readiness["scope"].review_notes == ("Scope fixture",)
    assert "get_filing_seed" in result.tool_calls and "get_price_history" in result.tool_calls
    # The model snapshot is opt-in: the default path makes no interpretation call.
    assert "get_sec_snapshot" not in result.tool_calls and result.financials is None
    assert all("get_fred_rate:" + series in result.tool_calls for series in SERIES)
    assert all("get_damodaran:" + dataset in result.tool_calls for dataset in FILES)
    assert result.benchmarks and result.review_required
    if outage is None:
        assert result.raw is not None and result.history_issues
        assert "get_financial_history" in result.tool_calls
        assert not result.quality.has_blocking_issues
        assert result.industry is not None and result.industry.method == "ticker"
        assert [r.name for r in result.benchmarks["betas"].rows] == ["Computers/Peripherals"]
        assert result.market.industry == "Computers/Peripherals" and result.market.sic == "3571"
        assert result.market.shares_observed_on == date(2025, 10, 17)
        assert result.market.market_cap.value == pytest.approx(
            result.market.close.price.value * result.market.shares_outstanding.value
        )
        assert result.macro is not None and len(result.rates) == 3
        invalid = result.model_dump()
        invalid["request"]["ticker"] = "MSFT"
        with pytest.raises(ValidationError, match="ticker/cutoff"):
            DataBundle.model_validate(invalid)
    else:
        assert result.quality.has_blocking_issues and result.quality.missing_fields
        if outage == "fred":
            assert result.rates == {} and result.macro is None
        if outage == "prices":
            assert result.prices is None and result.market is None
        if outage == "sec":
            assert result.raw is None and "get_financial_history" not in result.tool_calls
            assert result.market is not None  # shares come from the cover page, not the seed
        if outage == "industry":
            assert result.industry is None and "betas" not in result.benchmarks
            assert result.market is not None and result.market.industry is None
        if outage == "shares":
            assert result.market is None and result.raw is not None
        if outage == "history":
            assert result.raw is not None and result.market is not None
            assert "construction" in result.history_issues


def test_explicit_industry_skips_mapping(tmp_path, monkeypatch):
    result = run_agent(tmp_path, monkeypatch, industry="Computers/Peripherals")
    assert "get_industry" not in result.tool_calls
    assert result.industry is not None and result.industry.method == "user"
    unknown = run_agent(tmp_path, monkeypatch, industry="Not an industry")
    assert "betas" not in unknown.benchmarks and unknown.quality.has_blocking_issues


def test_saved_snapshot_is_a_cross_check_not_a_source(tmp_path, monkeypatch):
    agreed = run_agent(tmp_path, monkeypatch, saved=snapshot())
    assert agreed.financials is not None and "reuse_validated_sec_snapshot" in agreed.tool_calls
    check = next(c for c in agreed.quality.checks if c.field == "financials")
    assert check.status == "passed"
    # A disagreement is reported for review, but the package values stay the source.
    differs = run_agent(tmp_path, monkeypatch, saved=snapshot(), seed=filing_seed(1e10))
    check = next(c for c in differs.quality.checks if c.field == "financials")
    assert check.status == "failed" and check.severity == "warning"
    assert "revenue" in check.message and not differs.quality.has_blocking_issues
    assert differs.raw.annual[0].values["revenue"].value == pytest.approx(416161000000 + 1e10)


def test_cross_check_reports_a_period_mismatch():
    period = filing_seed().period.model_copy(update={"period_end": date(2025, 9, 28)})
    assert "differs from the 10-K period" in cross_check(snapshot().snapshot, period)[0]


def test_market_bridge_adjusts_shares_for_splits_and_rejects_opaque_basis(tmp_path):
    with PriceClient(configuration(tmp_path), loader=lambda *_: prices_body()) as client:
        prices = client.get_price_history("AAPL", START, CUTOFF)
    prices = prices.model_copy(
        update={"splits": (SplitObservation(observed_on=date(2026, 1, 1), ratio=2.0),)}
    )
    shares = cover()
    market = market_from_sources(shares.shares, shares.observed_on, prices, "Computers/Peripherals")
    assert market.shares_outstanding.value == shares.shares.value * 2
    with pytest.raises(DataUnavailable):
        market_from_sources(
            shares.shares,
            shares.observed_on,
            prices.model_copy(update={"source": "stooq"}),
            "industry",
        )
