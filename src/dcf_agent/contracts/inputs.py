"""Requests and dated input datasets; amounts use unscaled USD."""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dcf_agent.contracts.provenance import Sourced

Positive = Annotated[float, Field(gt=0)]
Intent = Literal["I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9", "I10", "I11"]
FinancialConcept = Literal[
    "revenue",
    "cogs",
    "sga",
    "rnd",
    "ebit",
    "depreciation_amortization",
    "stock_based_compensation",
    "interest_expense",
    "income_tax_expense",
    "pretax_income",
    "net_income",
    "cash",
    "marketable_securities",
    "receivables",
    "inventory",
    "payables",
    "net_ppe",
    "goodwill",
    "debt",
    "operating_lease_liabilities",
    "finance_lease_liabilities",
    "equity",
    "minority_interest",
    "preferred_equity",
    "total_assets",
    "total_liabilities",
    "operating_cash_flow",
    "capex",
    "buybacks",
    "dividends",
    "change_in_cash",
    "shares_outstanding",
]


class InputContract(BaseModel):
    """Shared validation policy for this group of input contracts."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


def validate_cutoff(value: Sourced[float], cutoff: date) -> None:
    """Reject evidence claiming an information cutoff after its dataset cutoff."""
    if value.prov.as_of > cutoff:
        raise ValueError("Evidence cutoff cannot be later than the dataset cutoff")


class ValuationRequest(InputContract):
    """Resolved intake; resolution and scope checking happen outside this model."""

    ticker: str = Field(min_length=1, pattern=r"^[A-Z0-9][A-Z0-9.\-]*$")
    cik: str = Field(pattern=r"^[0-9]{10}$")
    company_name: str = Field(min_length=1, pattern=r"\S")
    valuation_date: date
    mode: Literal["info", "quick", "full"]
    intent: Intent | None = None
    question: str | None = Field(default=None, min_length=1, pattern=r"\S")
    autopilot: bool = False
    plan: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if int(self.cik) == 0:
            raise ValueError("CIK cannot be zero")
        if self.mode == "info" and self.question is None:
            raise ValueError("Info mode requires a question")
        if any(not node.strip() for node in self.plan):
            raise ValueError("Plan node names cannot be blank")
        return self


class FinancialPeriod(InputContract):
    """Reported historical period with sourced values.

    USD amounts are unscaled dollars; shares are counts. Expenses, capex,
    buybacks, and dividends use positive magnitudes; profit and cash-flow
    measures remain signed. Stock values are measured at period_end, while
    flow values cover period_start through period_end.
    """

    kind: Literal["annual", "quarterly", "ttm"]
    fiscal_year: int = Field(ge=1900, le=9999)
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    period_start: date
    period_end: date
    values: dict[FinancialConcept, Sourced[float]] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if self.period_start > self.period_end:
            raise ValueError("Period start cannot follow period end")
        if (self.kind == "quarterly") != (self.fiscal_quarter is not None):
            raise ValueError("Only quarterly periods require a fiscal quarter")
        for concept, value in self.values.items():
            if value.prov.period_end != self.period_end:
                raise ValueError(f"{concept}: evidence period end must match the financial period")
        return self


class RawFinancials(InputContract):
    """Available reported history, including partial datasets.

    Missing concepts remain absent, never silently zero. Statement completeness,
    identities, and minimum history are adapter/DataQualityReport checks.
    """

    cik: str = Field(pattern=r"^[0-9]{10}$")
    as_of: date
    currency: Literal["USD"] = "USD"
    annual: tuple[FinancialPeriod, ...] = ()
    quarterly: tuple[FinancialPeriod, ...] = ()
    ttm: FinancialPeriod | None = None

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        if int(self.cik) == 0:
            raise ValueError("CIK cannot be zero")
        if not self.annual and not self.quarterly and self.ttm is None:
            raise ValueError("At least one historical period is required")
        groups = [("annual", self.annual), ("quarterly", self.quarterly)]
        if self.ttm is not None:
            groups.append(("ttm", (self.ttm,)))
        for kind, periods in groups:
            seen: set[tuple[date, date]] = set()
            for period in periods:
                if period.kind != kind:
                    raise ValueError(f"Expected {kind} period")
                identity = (period.period_start, period.period_end)
                if identity in seen:
                    raise ValueError("Duplicate historical period")
                seen.add(identity)
                if period.period_end > self.as_of:
                    raise ValueError("Historical period cannot end after the dataset cutoff")
                for value in period.values.values():
                    validate_cutoff(value, self.as_of)
        return self


class PriceObservation(InputContract):
    """A dated USD-per-share price; explicitly declare adjustment conventions."""

    observed_on: date
    price: Sourced[Positive]

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.observed_on > self.price.prov.as_of:
            raise ValueError("Price observation cannot follow its evidence cutoff")
        return self


class MarketData(InputContract):
    """Market snapshot; close must use the same share basis as the share count.

    Beta history may be adjusted; adapters must document corporate-action
    treatment and prevent future adjustment information leaking into history.
    Share observation date means when the share count was measured, not downloaded.
    """

    ticker: str = Field(min_length=1, pattern=r"^[A-Z0-9][A-Z0-9.\-]*$")
    as_of: date
    currency: Literal["USD"] = "USD"
    close: PriceObservation
    shares_outstanding: Sourced[Positive]
    shares_observed_on: date
    market_cap: Sourced[Positive] | None = None
    price_history: tuple[PriceObservation, ...] = ()
    history_basis: Literal["unadjusted", "split_adjusted", "total_return_adjusted"] = "unadjusted"
    sic: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    industry: str | None = Field(default=None, min_length=1, pattern=r"\S")

    @model_validator(mode="after")
    def validate_market_dates(self) -> Self:
        validate_cutoff(self.shares_outstanding, self.as_of)
        if self.shares_observed_on > self.shares_outstanding.prov.as_of:
            raise ValueError("Share observation cannot follow its evidence cutoff")
        if self.market_cap is not None:
            validate_cutoff(self.market_cap, self.as_of)
        for observation in (self.close, *self.price_history):
            validate_cutoff(observation.price, self.as_of)
        dates = [point.observed_on for point in self.price_history]
        if dates != sorted(set(dates)):
            raise ValueError("Price history must have unique dates in ascending order")
        return self


class MacroObservation(InputContract):
    """Dated rate or premium, expressed as a decimal (0.04 means 4%).

    available_on is when this version was published. observed_on identifies
    the measurement period; both matter for revised macro datasets.
    """

    observed_on: date
    available_on: date
    rate: Sourced[float]

    @model_validator(mode="after")
    def validate_macro_dates(self) -> Self:
        if self.observed_on > self.available_on:
            raise ValueError("Observation cannot follow publication")
        if self.available_on > self.rate.prov.as_of:
            raise ValueError("Publication cannot follow the evidence cutoff")
        return self


class MacroData(InputContract):
    """Dated macro inputs. Negative risk-free rates are structurally permitted."""

    as_of: date
    risk_free_rate: MacroObservation
    equity_risk_premium: MacroObservation
    country_risk_premium: MacroObservation
    credit_spread: MacroObservation | None = None
    inflation_expectation: MacroObservation | None = None

    @model_validator(mode="after")
    def validate_macro_inputs(self) -> Self:
        for observation in (
            self.risk_free_rate,
            self.equity_risk_premium,
            self.country_risk_premium,
            self.credit_spread,
            self.inflation_expectation,
        ):
            if observation is not None:
                validate_cutoff(observation.rate, self.as_of)
        for premium in (self.equity_risk_premium, self.country_risk_premium, self.credit_spread):
            if premium is not None and premium.rate.value < 0:
                raise ValueError("Risk premiums and credit spreads cannot be negative")
        return self
