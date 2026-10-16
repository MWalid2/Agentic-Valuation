"""Filing evidence, proposed adjustments, and normalized history."""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from dcf_agent.contracts.inputs import InputContract
from dcf_agent.contracts.provenance import Confidence, Provenance, Sourced

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
NonNegative = Annotated[float, Field(ge=0)]
Rate = Annotated[float, Field(ge=0, le=1)]


def evidence_records(value: object) -> tuple[Provenance, ...]:
    """Collect provenance from nested contract payloads."""
    if isinstance(value, Provenance):
        return (value,)
    if isinstance(value, BaseModel):
        return tuple(
            record
            for name in type(value).model_fields
            for record in evidence_records(getattr(value, name))
        )
    if isinstance(value, (tuple, list)):
        return tuple(record for item in value for record in evidence_records(item))
    if isinstance(value, dict):
        return tuple(record for item in value.values() for record in evidence_records(item))
    return ()


class FilingCitation(InputContract):
    """Location in an SEC filing; the locator is not independently authenticated."""

    accession: str = Field(pattern=r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
    filed: date
    section: Text
    page: Text | None = None
    table_id: Text | None = None


class FilingItem(InputContract):
    """Evidence-backed qualitative finding or quantified filing item."""

    item_id: Text
    description: Text
    citation: FilingCitation
    confidence: Confidence
    amount: Sourced[float] | None = None


class DebtInstrument(InputContract):
    """Amounts in USD; coupon as a decimal; unknown terms remain absent."""

    instrument_id: Text
    description: Text
    principal: Sourced[NonNegative]
    citation: FilingCitation
    coupon: Sourced[Rate] | None = None
    maturity: date | None = None
    fair_value: Sourced[NonNegative] | None = None


class LeasePayment(InputContract):
    """A future contractual payment, not a future-published input."""

    due_on: date
    payment: Sourced[NonNegative]


class LeaseDisclosure(InputContract):
    lease_type: Literal["operating", "finance"]
    liability: Sourced[NonNegative]
    citation: FilingCitation
    discount_rate: Sourced[Rate] | None = None
    payments: tuple[LeasePayment, ...] = ()


class DilutiveSecurity(InputContract):
    """Inputs to later TSM and if-converted calculations; no valuation here."""

    security_id: Text
    kind: Literal["option", "warrant", "rsu", "psu", "convertible"]
    count: Sourced[NonNegative]
    citation: FilingCitation
    strike_price: Sourced[NonNegative] | None = None
    principal: Sourced[NonNegative] | None = None
    conversion_shares: Sourced[NonNegative] | None = None

    @model_validator(mode="after")
    def validate_terms(self) -> Self:
        if self.kind in ("option", "warrant") and self.strike_price is None:
            raise ValueError("Options and warrants require a strike price")
        if self.kind == "convertible" and (
            self.principal is None or self.conversion_shares is None
        ):
            raise ValueError("Convertibles require principal and conversion shares")
        return self


class SegmentData(InputContract):
    name: Text
    period_end: date
    revenue: Sourced[float]
    citation: FilingCitation
    operating_income: Sourced[float] | None = None

    @model_validator(mode="after")
    def validate_segment_period(self) -> Self:
        for record in evidence_records(self):
            if record.period_end != self.period_end:
                raise ValueError("Segment evidence must match its period end")
        return self


class FilingInsights(InputContract):
    """Partial filing findings. Empty collections mean no recorded findings.

    They do not prove that an item is absent from the business. Extraction
    completeness must be recorded in the quality report.
    """

    cik: str = Field(pattern=r"^[0-9]{10}$")
    as_of: date
    debt_schedule: tuple[DebtInstrument, ...] = ()
    lease_liabilities: tuple[LeaseDisclosure, ...] = ()
    dilutive_securities: tuple[DilutiveSecurity, ...] = ()
    segment_data: tuple[SegmentData, ...] = ()
    management_guidance: tuple[FilingItem, ...] = ()
    one_off_items: tuple[FilingItem, ...] = ()
    pension_obligations: tuple[FilingItem, ...] = ()
    contingent_liabilities: tuple[FilingItem, ...] = ()
    non_operating_assets: tuple[FilingItem, ...] = ()
    risk_flags: tuple[FilingItem, ...] = ()
    # The marginal tax rate read from the rate reconciliation, with its evidence.
    tax_items: tuple[FilingItem, ...] = ()
    # Captive-finance debt read from a consolidating statement, one item per period.
    captive_finance: tuple[FilingItem, ...] = ()

    @model_validator(mode="after")
    def validate_insight_dates(self) -> Self:
        if int(self.cik) == 0:
            raise ValueError("CIK cannot be zero")
        for record in evidence_records(self):
            if record.as_of > self.as_of:
                raise ValueError("Evidence cutoff cannot follow the insights cutoff")
        for name in type(self).model_fields:
            group = getattr(self, name)
            if not isinstance(group, tuple):
                continue
            for item in group:
                if item.citation.filed > self.as_of:
                    raise ValueError("Cited filing cannot follow the insights cutoff")
                for record in evidence_records(item):
                    if record.source in ("sec_xbrl", "sec_filing_text"):
                        if record.filed != item.citation.filed:
                            raise ValueError("SEC evidence and citation filing dates must agree")
                        if item.citation.accession not in record.ref:
                            raise ValueError(
                                "SEC evidence reference must identify the cited accession"
                            )
        return self


class Adjustment(InputContract):
    """A proposed treatment; approval and application happen in later components."""

    adjustment_id: Text
    item: Text
    period_end: date
    as_of: date
    target: Literal["ebit", "invested_capital", "debt", "cash", "pension_deficit"]
    amount: Sourced[NonNegative]
    direction: Literal["increase", "decrease"]
    rationale: Text
    evidence: tuple[FilingCitation, ...] = Field(min_length=1)
    reference_ebit: Sourced[float]
    confidence: Confidence
    ambiguous: bool = False

    @model_validator(mode="after")
    def validate_adjustment_evidence(self) -> Self:
        if self.period_end > self.as_of:
            raise ValueError("Historical adjustment period cannot follow cutoff")
        for record in evidence_records(self):
            if record.as_of > self.as_of or record.period_end != self.period_end:
                raise ValueError("Adjustment evidence must match its cutoff and period")
        if any(citation.filed > self.as_of for citation in self.evidence):
            raise ValueError("Adjustment citation cannot follow cutoff")
        return self

    @property
    def materiality_pct_ebit(self) -> float | None:
        """Decimal ratio to absolute EBIT; undefined when EBIT is zero."""
        if self.reference_ebit.value == 0:
            return None
        return self.amount.value / abs(self.reference_ebit.value)

    @property
    def requires_review(self) -> bool:
        """Gate 1: above 3%, low confidence, ambiguity, or undefined materiality."""
        ratio = self.materiality_pct_ebit
        low_evidence = any(record.confidence == "low" for record in evidence_records(self))
        return (
            self.confidence == "low"
            or low_evidence
            or self.ambiguous
            or ratio is None
            or ratio > 0.03
        )


class NormalizedPeriod(InputContract):
    """Sourced economic metrics for one annual or TTM period.

    Calculators must establish identities such as NOPAT and ROIC and record
    their input lineage. These contracts do not recompute financial results.
    """

    kind: Literal["annual", "ttm"]
    period_start: date
    period_end: date
    revenue: Sourced[float]
    clean_ebit: Sourced[float]
    nopat: Sourced[float]
    invested_capital: Sourced[float]
    effective_tax_rate: Sourced[float]
    marginal_tax_rate: Sourced[Rate]
    ebitda: Sourced[float] | None = None
    operating_nwc: Sourced[float] | None = None
    net_ppe: Sourced[float] | None = None
    roic: Sourced[float] | None = None
    sales_to_capital: Sourced[float] | None = None
    reinvestment: Sourced[float] | None = None
    reinvestment_rate: Sourced[float] | None = None
    adjustment_ids: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def validate_normalized_period(self) -> Self:
        if self.period_start > self.period_end:
            raise ValueError("Period start cannot follow period end")
        if len(set(self.adjustment_ids)) != len(self.adjustment_ids):
            raise ValueError("Adjustment IDs must be unique within a period")
        for record in evidence_records(self):
            if record.period_end != self.period_end:
                raise ValueError("Normalized evidence must match the period end")
        return self


class NormalizedFinancials(InputContract):
    cik: str = Field(pattern=r"^[0-9]{10}$")
    as_of: date
    currency: Literal["USD"] = "USD"
    annual: tuple[NormalizedPeriod, ...] = ()
    ttm: NormalizedPeriod | None = None
    sbc_treatment: Literal["expense"] = "expense"
    leases_as_debt: bool = True
    capitalize_rnd: bool = False

    @model_validator(mode="after")
    def validate_normalized_history(self) -> Self:
        if int(self.cik) == 0:
            raise ValueError("CIK cannot be zero")
        if not self.annual and self.ttm is None:
            raise ValueError("At least one normalized period is required")
        seen: set[tuple[date, date]] = set()
        for period in self.annual:
            if period.kind != "annual":
                raise ValueError("Annual history requires annual periods")
            identity = (period.period_start, period.period_end)
            if identity in seen:
                raise ValueError("Duplicate normalized annual period")
            seen.add(identity)
        if self.ttm is not None and self.ttm.kind != "ttm":
            raise ValueError("TTM requires a TTM period")
        periods = (*self.annual, *((self.ttm,) if self.ttm is not None else ()))
        if any(period.period_end > self.as_of for period in periods):
            raise ValueError("Normalized history cannot follow cutoff")
        if any(record.as_of > self.as_of for record in evidence_records(self)):
            raise ValueError("Normalized evidence cannot follow cutoff")
        return self
