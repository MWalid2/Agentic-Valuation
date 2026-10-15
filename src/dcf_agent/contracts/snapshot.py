"""Single-period extraction boundary; missing evidence is never a zero."""

from datetime import date
from typing import Literal, Self

from pydantic import Field, model_validator

from dcf_agent.contracts.inputs import InputContract
from dcf_agent.contracts.provenance import Sourced

SnapshotConcept = Literal[
    "revenue",
    "operating_income",
    "net_income",
    "cash",
    "debt",
    "total_assets",
    "total_liabilities",
    "equity",
    "operating_cash_flow",
    "capex",
    "shares_outstanding",
]
SNAPSHOT_CONCEPTS: tuple[SnapshotConcept, ...] = (
    "revenue",
    "operating_income",
    "net_income",
    "cash",
    "debt",
    "total_assets",
    "total_liabilities",
    "equity",
    "operating_cash_flow",
    "capex",
    "shares_outstanding",
)
FLOWS = {"revenue", "operating_income", "net_income", "operating_cash_flow", "capex"}


class SnapshotField(InputContract):
    status: Literal["available", "missing", "ambiguous"]
    amount: Sourced[float] | None = None
    unit: Literal["USD", "shares"]
    basis: Literal["instant", "period", "ytd"]
    period_start: date | None = None
    observed_on: date | None = None
    explanation: str | None = Field(default=None, min_length=1, pattern=r"\S")

    @model_validator(mode="after")
    def validate_field(self) -> Self:
        if self.status == "available":
            if self.amount is None or self.observed_on is None:
                raise ValueError(
                    "Available fields require an amount and observation/period-end date"
                )
            if self.amount.prov.period_end != self.observed_on:
                raise ValueError("Evidence period end must match the observation date")
            if self.basis == "instant" and self.period_start is not None:
                raise ValueError("Instant values cannot have a duration start")
            if self.basis != "instant" and (
                self.period_start is None or self.period_start > self.observed_on
            ):
                raise ValueError("Flow values require a valid duration start")
        elif self.amount is not None or self.explanation is None:
            raise ValueError(
                "Missing/ambiguous fields require an explanation and no accepted amount"
            )
        return self


class FinancialSnapshot(InputContract):
    cik: str = Field(pattern=r"^[0-9]{10}$")
    as_of: date
    kind: Literal["annual", "quarterly"]
    fiscal_year: int = Field(ge=1900, le=9999)
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    period_start: date
    period_end: date
    fields: dict[SnapshotConcept, SnapshotField]
    review_required: Literal[True] = True

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if int(self.cik) == 0 or not self.period_start <= self.period_end <= self.as_of:
            raise ValueError("Snapshot requires a nonzero CIK and eligible period dates")
        if (self.kind == "quarterly") != (self.fiscal_quarter is not None):
            raise ValueError("Only quarterly snapshots require a fiscal quarter")
        if set(self.fields) != set(SNAPSHOT_CONCEPTS):
            raise ValueError("Every core field must be explicitly available, missing, or ambiguous")
        for concept, field in self.fields.items():
            if field.unit != ("shares" if concept == "shares_outstanding" else "USD"):
                raise ValueError(f"{concept}: incorrect unit")
            if (concept in FLOWS) == (field.basis == "instant"):
                raise ValueError(f"{concept}: incorrect stock/flow basis")
            if field.status != "available":
                continue
            assert field.amount is not None and field.observed_on is not None
            prov = field.amount.prov
            if prov.as_of > self.as_of or field.observed_on > self.as_of:
                raise ValueError(f"{concept}: evidence after the snapshot cutoff")
            if prov.source not in ("sec_xbrl", "sec_filing_text", "derived") or prov.filed is None:
                raise ValueError(f"{concept}: SEC filing evidence or dated derivation required")
            if concept != "shares_outstanding" and field.observed_on != self.period_end:
                raise ValueError(f"{concept}: reporting period end mismatch")
            if field.basis == "period" and field.period_start != self.period_start:
                raise ValueError(f"{concept}: reporting period start mismatch")
            if field.basis == "ytd":
                if self.kind == "annual" and field.period_start != self.period_start:
                    raise ValueError("Annual YTD cash flows must cover the annual reporting period")
                if (
                    concept not in ("operating_cash_flow", "capex")
                    or field.period_start is None
                    or field.period_start > self.period_start
                ):
                    raise ValueError(
                        "Only cash-flow fields may explicitly retain earlier YTD starts"
                    )
            if (
                concept in ("cash", "debt", "total_assets", "total_liabilities", "capex")
                and field.amount.value < 0
            ):
                raise ValueError(f"{concept}: expected a nonnegative magnitude")
            if concept == "shares_outstanding" and field.amount.value <= 0:
                raise ValueError("Shares outstanding must be positive")
        return self

    @property
    def is_complete(self) -> bool:
        """Presence only; does not establish accounting correctness or approval."""
        return all(field.status == "available" for field in self.fields.values())
