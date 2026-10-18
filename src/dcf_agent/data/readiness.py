"""Consumer-specific readiness from concept statuses; an absent disclosure is never zero.

A required concept is satisfied when it is reported, carried forward (flagged),
or evidenced as not presented where an evidenced zero is meaningful. Each
consumer accepts ``not_disclosed`` only for concepts with a documented fallback.
"""

from dcf_agent.contracts.external import InputReadiness
from dcf_agent.contracts.inputs import (
    ConceptStatus,
    FinancialConcept,
    FinancialPeriod,
    RawFinancials,
    within_other_lines,
)
from dcf_agent.data.history import measurement_basis

REQUIRED: dict[str, set[FinancialConcept]] = {
    "normalization": {
        "revenue",
        "ebit",
        "pretax_income",
        "income_tax_expense",
        "receivables",
        "inventory",
        "payables",
        "net_ppe",
        "depreciation_amortization",
        "operating_lease_liabilities",
    },
    "wacc": {
        "ebit",
        "interest_expense",
        "debt",
        "captive_finance_debt",
        "equity",
        "pretax_income",
        "income_tax_expense",
    },
    "equity_bridge": {
        "cash",
        "debt",
        "marketable_securities",
        "minority_interest",
        "preferred_equity",
        "temporary_equity",
        "captive_finance_debt",
    },
}
# An evidenced absence cannot stand in for these: every operating company reports them.
MUST_REPORT: set[FinancialConcept] = {"revenue", "ebit", "pretax_income", "equity", "cash"}
# Documented fallbacks when a filing does not disclose the concept at all.
UNDISCLOSED_FALLBACK: dict[str, dict[FinancialConcept, str]] = {
    "normalization": {
        "operating_lease_liabilities": "no lease disclosure: no lease capitalization adjustment",
    },
    "wacc": {
        "interest_expense": "interest not disclosed: cost of debt from rating/spread, "
        "no coverage ratio",
    },
    "equity_bridge": {},
}
MIN_SHORT_HISTORY = 2
_ABSENT = ConceptStatus(status="unresolved")


def assess_readiness(
    raw: RawFinancials | None, history_issues: dict[str, str]
) -> dict[str, InputReadiness]:
    reports = {}
    for consumer, required in REQUIRED.items():
        missing: dict[str, tuple[str, ...]] = {}
        notes: list[str] = []
        periods: tuple[FinancialPeriod, ...]
        if raw is None:
            missing["raw"] = ("RawFinancials",)
            periods = ()
        else:
            periods = (
                (*raw.annual, *((raw.ttm,) if raw.ttm else ()))
                if consumer == "normalization"
                else ((raw.ttm,) if raw.ttm else ())
            )
            if raw.ttm is None:
                missing["history"] = ("TTM",)
            if consumer == "normalization" and len(raw.annual) < 5:
                short = "no earlier annual reports" in history_issues.get("annual_history", "")
                if short and len(raw.annual) >= MIN_SHORT_HISTORY:
                    notes.append(
                        f"Short history: {len(raw.annual)} annual periods; the registrant "
                        "has no earlier annual reports"
                    )
                else:
                    missing["annual_history"] = ("Five annual periods",)
        latest_annual = raw.annual[-1] if raw is not None and raw.annual else None
        if (
            latest_annual is not None
            and "revenue" not in latest_annual.values
            and latest_annual.statuses.get("revenue", _ABSENT).status == "not_presented"
        ):
            missing["scope"] = ("Pre-revenue or shell company: outside the v1 DCF scope",)
        for period in periods:
            absent = []
            for concept in sorted(required):
                status = period.statuses.get(concept)
                label = f"{period.kind}:{period.period_end}:{concept}"
                if concept in period.values:
                    if status is not None and status.status == "carried_forward":
                        notes.append(f"{label} carried forward from {status.observed_on}")
                    elif status is not None and status.basis:
                        notes.append(f"{label} basis: {status.basis}")
                    continue
                kind = status.status if status is not None else "missing"
                if kind == "not_presented" and concept not in MUST_REPORT:
                    notes.append(f"{label} not presented (evidenced zero)")
                elif concept == "marketable_securities" and within_other_lines(status):
                    notes.append(f"{label} inside other statement lines: none deducted")
                elif kind == "not_disclosed" and concept in UNDISCLOSED_FALLBACK[consumer]:
                    notes.append(f"{label}: {UNDISCLOSED_FALLBACK[consumer][concept]}")
                else:
                    absent.append(f"{concept} ({kind})")
            if absent:
                missing[f"{period.kind}:{period.period_end}"] = tuple(absent)
        # A series with mixed bases would distort changes (e.g. working capital).
        mixed = tuple(
            sorted(
                concept
                for concept in required
                if len(
                    {
                        measurement_basis(
                            p.statuses[concept].basis if concept in p.statuses else None
                        )
                        for p in periods
                        if concept in p.values
                    }
                )
                > 1
            )
        )
        if mixed:
            missing["basis_inconsistent"] = mixed
        gaps = tuple(
            k for k in ("annual_continuity", "quarterly_continuity") if k in history_issues
        )
        if gaps:
            missing["unresolved_checks"] = gaps
        tie_outs = [k for k in history_issues if k.endswith(":tie_out")]
        if tie_outs:
            notes.append(f"{len(tie_outs)} reported quarters differ from cumulative subtraction")
        reports[consumer] = InputReadiness(
            status="blocked" if missing else "needs_review",
            missing=missing,
            review_notes=(
                "Numeric coverage only; calculations and human approval remain separate.",
                "Marginal tax, lease adjustments, vintage comparability and dated "
                "market shares need downstream review.",
                *notes,
            ),
        )
    return reports
