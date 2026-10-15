"""Typed external-source results and the combined Data Agent handoff."""

from datetime import date
from typing import Literal, Self

from pydantic import Field, model_validator

from dcf_agent.contracts.inputs import (
    FinancialConcept,
    InputContract,
    MacroData,
    MacroObservation,
    MarketData,
    PriceObservation,
    RawFinancials,
    ValuationRequest,
)
from dcf_agent.contracts.provenance import Sourced
from dcf_agent.contracts.quality import DataQualityReport
from dcf_agent.contracts.snapshot import FinancialSnapshot

Dataset = Literal["betas", "margins", "sales_to_capital", "country_risk", "spreads"]


class BenchmarkRow(InputContract):
    name: str
    values: dict[str, Sourced[float]]
    units: dict[str, str]
    text: dict[str, str] = Field(default_factory=dict)


class BenchmarkTable(InputContract):
    dataset: Dataset
    as_of: date
    analysis_on: date | None
    available_on: date
    retrieved_on: date
    ref: str
    version: str
    rows: tuple[BenchmarkRow, ...]
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        if self.available_on > min(self.as_of, self.retrieved_on):
            raise ValueError("Benchmark version is not eligible at its cutoff/retrieval date")
        if self.analysis_on is not None and self.analysis_on > self.available_on:
            raise ValueError("Benchmark analysis date follows its availability")
        for row in self.rows:
            if set(row.values) != set(row.units):
                raise ValueError("Every benchmark value requires a unit")
            if any(v.prov.as_of > self.as_of for v in row.values.values()):
                raise ValueError("Benchmark evidence exceeds cutoff")
        return self


class SplitObservation(InputContract):
    observed_on: date
    ratio: float = Field(gt=0)


class PriceHistory(InputContract):
    ticker: str
    as_of: date
    start: date
    retrieved_on: date
    currency: Literal["USD"] = "USD"
    source: Literal["yfinance", "stooq"]
    basis: Literal["split_adjusted", "provider_adjusted"]
    observations: tuple[PriceObservation, ...] = Field(min_length=1)
    splits: tuple[SplitObservation, ...] = ()
    point_in_time_verified: bool = False
    from_cache: bool = False
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        days = [p.observed_on for p in self.observations]
        if days != sorted(set(days)) or not self.start <= days[0] <= days[-1] <= self.as_of:
            raise ValueError("Price dates must be unique, sorted, and within the request")
        if any(p.price.prov.as_of > self.as_of for p in self.observations):
            raise ValueError("Price evidence exceeds cutoff")
        if any(not self.start <= s.observed_on <= self.as_of for s in self.splits):
            raise ValueError("Split observation outside requested interval")
        return self


class IndustrySelection(InputContract):
    """Which Damodaran industry the company was assigned to, and how."""

    name: str = Field(min_length=1)
    method: Literal["ticker", "sic", "user"]
    sic: str | None = None
    alternatives: tuple[str, ...] = ()
    ref: str | None = None
    note: str = ""


class InputReadiness(InputContract):
    """Numeric coverage is distinct from approval or valuation readiness."""

    status: Literal["blocked", "needs_review"]
    missing: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    review_notes: tuple[str, ...]

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if (self.status == "blocked") != bool(self.missing):
            raise ValueError("Readiness status must agree with missing required inputs")
        return self


class DataBundle(InputContract):
    request: ValuationRequest
    financials: FinancialSnapshot | None = None
    raw: RawFinancials | None = None
    raw_issues: dict[FinancialConcept, str] = Field(default_factory=dict)
    history_issues: dict[str, str] = Field(default_factory=dict)
    readiness: dict[str, InputReadiness] = Field(default_factory=dict)
    prices: PriceHistory | None = None
    market: MarketData | None = None
    macro: MacroData | None = None
    rates: dict[str, MacroObservation] = Field(default_factory=dict)
    benchmarks: dict[Dataset, BenchmarkTable] = Field(default_factory=dict)
    industry: IndustrySelection | None = None
    quality: DataQualityReport
    tool_calls: tuple[str, ...] = ()
    # Source fingerprint of the code that built this bundle; None marks a legacy bundle.
    build: str | None = None
    review_required: Literal[True] = True

    @model_validator(mode="after")
    def validate_request_boundary(self) -> Self:
        cutoff = self.request.valuation_date
        if self.quality.as_of != cutoff:
            raise ValueError("Quality report cutoff differs from request")
        if self.raw is not None and (self.raw.cik != self.request.cik or self.raw.as_of != cutoff):
            raise ValueError("Raw financials do not match request identity/cutoff")
        if self.financials is not None and (
            self.financials.cik != self.request.cik or self.financials.as_of != cutoff
        ):
            raise ValueError("Financial snapshot does not match request identity/cutoff")
        for item in (self.prices, self.market):
            if item is not None and (item.ticker != self.request.ticker or item.as_of != cutoff):
                raise ValueError("Market input does not match request ticker/cutoff")
        if self.macro is not None and self.macro.as_of != cutoff:
            raise ValueError("Macro cutoff differs from request")
        if any(t.as_of != cutoff for t in self.benchmarks.values()) or any(
            r.rate.prov.as_of > cutoff for r in self.rates.values()
        ):
            raise ValueError("External evidence exceeds or differs from request cutoff")
        return self
