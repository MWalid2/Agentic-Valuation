"""Evidence attached to sourced inputs and assumptions."""

from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Source = Literal[
    "sec_xbrl",
    "sec_filing_text",
    "fred",
    "damodaran",
    "yfinance",
    "stooq",
    "derived",
    "llm_estimate",
    "user_override",
]
Confidence = Literal["high", "medium", "low"]


class Provenance(BaseModel):
    """Origin of a value, eligible at the stated information cutoff.

    ``as_of`` is the information cutoff, usually the valuation date, rather than
    the download date. ``period_end`` is the period described by the value and
    may be future-dated for guidance or forecast assumptions. ``filed`` is the
    publication date of an SEC filing and is required for SEC sources.

    For other sources, adapters must check observation and release dates against
    the cutoff. This contract alone cannot prove point-in-time eligibility.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source: Source
    ref: str = Field(min_length=1, pattern=r"\S")
    as_of: date
    confidence: Confidence
    period_end: date | None = None
    filed: date | None = None
    note: str | None = Field(default=None, min_length=1, pattern=r"\S")

    @model_validator(mode="after")
    def validate_filing_date(self) -> Self:
        if self.source in ("sec_xbrl", "sec_filing_text") and self.filed is None:
            raise ValueError("SEC sources require a filing date")
        if self.filed is not None and self.filed > self.as_of:
            raise ValueError("Filing date must be on or before the information cutoff")
        return self


class Sourced[T](BaseModel):
    """A typed value paired with mandatory evidence.

    Use a concrete specialization such as ``Sourced[float]`` at boundaries.
    Non-finite numbers are rejected. Units and financial bounds belong to the
    financial contracts that will use this wrapper.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    value: T
    prov: Provenance
