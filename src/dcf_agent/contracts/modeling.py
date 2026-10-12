"""Forecast and result boundaries; calculations belong to the engine."""

from datetime import date
from math import isclose
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from dcf_agent.contracts.accounting import NonNegative, Rate, Text, evidence_records
from dcf_agent.contracts.inputs import InputContract, Positive
from dcf_agent.contracts.provenance import Sourced

Scenario = Literal["bear", "base", "bull"]
Growth = Annotated[float, Field(gt=-1)]


def check_evidence_cutoff(value: InputContract, cutoff: date) -> None:
    if any(record.as_of > cutoff for record in evidence_records(value)):
        raise ValueError("Evidence cannot follow the model cutoff")


class ForecastAssumptions(InputContract):
    """One scenario; decimal rates and dated, sourced annual drivers.

    Policy bounds needing macro/industry inputs are checked outside this model.
    Negative margins and growth are allowed; revenue cannot shrink by 100%.
    """

    as_of: date
    scenario: Scenario
    probability: Rate
    year_ends: tuple[date, ...] = Field(min_length=1)
    revenue_growth: tuple[Sourced[Growth], ...]
    operating_margin: tuple[Sourced[float], ...]
    sales_to_capital: tuple[Sourced[Positive], ...]
    tax_rate: tuple[Sourced[Rate], ...]
    terminal_growth: Sourced[Growth]
    terminal_roic: Sourced[Positive]
    marginal_tax_rate: Sourced[Rate]
    narrative: Text
    rationale: dict[Text, Text] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_forecast(self) -> Self:
        if list(self.year_ends) != sorted(set(self.year_ends)):
            raise ValueError("Forecast year ends must be unique and ascending")
        if self.year_ends[0] <= self.as_of:
            raise ValueError("Forecast must start after its information cutoff")
        drivers = (self.revenue_growth, self.operating_margin, self.sales_to_capital, self.tax_rate)
        for driver in drivers:
            if len(driver) != len(self.year_ends):
                raise ValueError("Forecast driver lengths must match year ends")
            for end, value in zip(self.year_ends, driver, strict=True):
                if value.prov.period_end != end:
                    raise ValueError("Forecast driver evidence must match its year end")
        if not isclose(self.tax_rate[-1].value, self.marginal_tax_rate.value, abs_tol=1e-9):
            raise ValueError("Final explicit tax rate must converge to marginal tax rate")
        check_evidence_cutoff(self, self.as_of)
        return self


class ForecastScenarios(InputContract):
    """Exactly one bear/base/bull, sharing cutoff and horizon."""

    scenarios: tuple[ForecastAssumptions, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_scenarios(self) -> Self:
        if {item.scenario for item in self.scenarios} != {"bear", "base", "bull"}:
            raise ValueError("Require one bear, base, and bull scenario")
        first = self.scenarios[0]
        if any(
            item.as_of != first.as_of or item.year_ends != first.year_ends
            for item in self.scenarios
        ):
            raise ValueError("Scenarios must share cutoff and forecast horizon")
        if not isclose(sum(item.probability for item in self.scenarios), 1.0, abs_tol=1e-9):
            raise ValueError("Scenario probabilities must sum to one")
        return self


class WACCResult(InputContract):
    """Sourced build-up and derived outputs; engine verifies arithmetic identities."""

    as_of: date
    rf: Sourced[float]
    erp: Sourced[NonNegative]
    crp: Sourced[NonNegative]
    unlevered_beta: Sourced[float]
    relevered_beta: Sourced[float]
    bottom_up_beta: Sourced[float]
    regression_beta: Sourced[float] | None = None
    beta_method: Literal["bottom_up", "regression", "average"]
    cost_equity: Sourced[float]
    pretax_kd: Sourced[float]
    tax_rate: Sourced[Rate]
    aftertax_kd: Sourced[float]
    weight_equity: Sourced[Rate]
    weight_debt: Sourced[Rate]
    wacc: Sourced[float]
    terminal_wacc: Sourced[float]
    wacc_band: tuple[float, float]
    method_notes: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_wacc(self) -> Self:
        if not isclose(self.weight_equity.value + self.weight_debt.value, 1.0, abs_tol=1e-9):
            raise ValueError("Capital weights must sum to one")
        if self.beta_method != "bottom_up" and self.regression_beta is None:
            raise ValueError("Selected beta method requires regression beta")
        low, high = self.wacc_band
        if not low <= self.wacc.value <= high:
            raise ValueError("WACC band must be ordered and contain WACC")
        if min(self.wacc.value, self.terminal_wacc.value) <= -1:
            raise ValueError("Discount rates must exceed -100%")
        check_evidence_cutoff(self, self.as_of)
        return self


class ValuationResult(InputContract):
    """Engine output with explicit discount times and auditable result references.

    Bridge entries are signed contributions to equity. Negative enterprise and
    equity values remain representable; undefined metrics are omitted, not zero.
    """

    as_of: date
    scenario: Scenario
    calculation_ref: Text
    input_hash: Text
    year_ends: tuple[date, ...] = Field(min_length=1)
    fcff_by_year: tuple[float, ...]
    discount_times: tuple[NonNegative, ...]
    discount_factors: tuple[Positive, ...]
    pv_explicit: float
    terminal_value: float
    terminal_discount_time: NonNegative
    pv_terminal: float
    enterprise_value: float
    bridge: dict[Text, Sourced[float]]
    equity_value: float
    diluted_shares: Positive
    value_per_share: float
    tv_share_of_ev: float | None
    implied: dict[Text, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_valuation(self) -> Self:
        for series in (self.fcff_by_year, self.discount_times, self.discount_factors):
            if len(series) != len(self.year_ends):
                raise ValueError("Valuation series must match forecast horizon")
        if list(self.year_ends) != sorted(set(self.year_ends)) or self.year_ends[0] <= self.as_of:
            raise ValueError("Valuation year ends must be future, unique, and ascending")
        if list(self.discount_times) != sorted(set(self.discount_times)):
            raise ValueError("Discount times must be unique and ascending")
        if self.terminal_discount_time < self.discount_times[-1]:
            raise ValueError("Terminal discount time cannot precede the final cash flow")
        if self.enterprise_value == 0 and self.tv_share_of_ev is not None:
            raise ValueError("Terminal share is undefined for zero enterprise value")
        check_evidence_cutoff(self, self.as_of)
        return self


class PeerMultiples(InputContract):
    """Undefined or unsuitable multiples remain None; ratios are unitless."""

    ticker: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.\-]*$")
    cik: str = Field(pattern=r"^[0-9]{10}$")
    as_of: date
    selection_rationale: Text
    ev_ebitda: Sourced[float] | None = None
    ev_sales: Sourced[float] | None = None
    ev_ebit: Sourced[float] | None = None
    price_earnings: Sourced[float] | None = None

    @model_validator(mode="after")
    def validate_peer(self) -> Self:
        if int(self.cik) == 0:
            raise ValueError("CIK cannot be zero")
        check_evidence_cutoff(self, self.as_of)
        return self


class MultipleSummary(InputContract):
    metric: Literal["ev_ebitda", "ev_sales", "ev_ebit", "price_earnings"]
    sample_size: int = Field(ge=1)
    first_quartile: float
    median: float
    third_quartile: float

    @model_validator(mode="after")
    def validate_quartiles(self) -> Self:
        if not self.first_quartile <= self.median <= self.third_quartile:
            raise ValueError("Quartiles must be ordered around the median")
        return self


class CompsResult(InputContract):
    """Partial comparisons are valid with explicit warnings and no invented range."""

    as_of: date
    peers: tuple[PeerMultiples, ...]
    summaries: tuple[MultipleSummary, ...] = ()
    implied_value_range: tuple[float, float] | None = None
    divergence_vs_dcf: float | None = None
    calculation_ref: Text
    warnings: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def validate_comps(self) -> Self:
        if len({peer.ticker for peer in self.peers}) != len(self.peers):
            raise ValueError("Peer tickers must be unique")
        if any(peer.as_of != self.as_of for peer in self.peers):
            raise ValueError("Peer cutoffs must match comparison cutoff")
        if len({item.metric for item in self.summaries}) != len(self.summaries):
            raise ValueError("Summary metrics must be unique")
        for summary in self.summaries:
            available = sum(getattr(peer, summary.metric) is not None for peer in self.peers)
            if summary.sample_size > available:
                raise ValueError("Summary sample cannot exceed available peer multiples")
            if summary.sample_size < 5 and not self.warnings:
                raise ValueError("Small multiple samples require a warning")
        if len(self.peers) < 5 and not self.warnings:
            raise ValueError("Fewer than five peers requires a warning")
        if self.implied_value_range is not None:
            if not self.summaries:
                raise ValueError("An implied range requires a multiple summary")
            if self.implied_value_range[0] > self.implied_value_range[1]:
                raise ValueError("Implied value range must be ordered")
        return self
