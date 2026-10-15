"""Conservative statement enrichment and an explicit single-period raw handoff."""

from typing import get_args

from dcf_agent.contracts.inputs import FinancialConcept, FinancialPeriod, RawFinancials
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.contracts.snapshot import FinancialSnapshot
from dcf_agent.data.sec import SECError
from dcf_agent.tools.edgar import EDGARTools, EvidenceCell
from dcf_agent.tools.snapshot import Section, read_statement

# Alternatives are equivalent totals, never components to add together.
RULES: dict[FinancialConcept, tuple[Section, tuple[str, ...]]] = {
    "cogs": ("income_statement", ("CostOfRevenue", "CostOfGoodsAndServicesSold")),
    "sga": ("income_statement", ("SellingGeneralAndAdministrativeExpense",)),
    "rnd": ("income_statement", ("ResearchAndDevelopmentExpense",)),
    "interest_expense": (
        "income_statement",
        ("InterestExpense", "InterestAndDebtExpense", "InterestExpenseNonOperating"),
    ),
    "income_tax_expense": ("income_statement", ("IncomeTaxExpenseBenefit",)),
    "pretax_income": (
        "income_statement",
        (
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        ),
    ),
    "depreciation_amortization": ("cash_flow", ("DepreciationDepletionAndAmortization",)),
    "stock_based_compensation": ("cash_flow", ("ShareBasedCompensation",)),
    "receivables": ("balance_sheet", ("AccountsReceivableNetCurrent",)),
    "inventory": ("balance_sheet", ("InventoryNet",)),
    "payables": ("balance_sheet", ("AccountsPayableCurrent", "AccountsPayableTradeCurrent")),
    "net_ppe": ("balance_sheet", ("PropertyPlantAndEquipmentNet",)),
    "goodwill": ("balance_sheet", ("Goodwill",)),
    "minority_interest": ("balance_sheet", ("MinorityInterest",)),
    "buybacks": ("cash_flow", ("PaymentsForRepurchaseOfCommonStock",)),
    "dividends": ("cash_flow", ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends")),
    "change_in_cash": (
        "cash_flow",
        ("CashAndCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",),
    ),
}
LEASES: dict[FinancialConcept, str] = {
    "operating_lease_liabilities": "OperatingLeaseLiability",
    "finance_lease_liabilities": "FinanceLeaseLiability",
}
TOTAL_COMPONENTS: dict[FinancialConcept, str] = {
    **LEASES,
    "marketable_securities": "MarketableSecurities",
}


def build_raw_financials(
    snapshot: FinancialSnapshot, tools: EDGARTools
) -> tuple[RawFinancials, dict[FinancialConcept, str], tuple[str, ...]]:
    """Read supported primary-statement totals; missing concepts stay absent.

    This does not build historical periods, subtract YTD cash flows, or treat
    cover-page shares as fiscal-end shares. Unknown tags require later review.
    """
    if (snapshot.cik, snapshot.as_of, snapshot.period_end) != (
        tools.cik,
        tools.as_of,
        tools.filing.report_date,
    ):
        raise SECError("Raw extraction identity/cutoff/period differs from snapshot")
    values: dict[FinancialConcept, Sourced[float]] = {}
    issues: dict[FinancialConcept, str] = {}
    warnings: list[str] = []
    for concept in get_args(FinancialConcept):
        field = snapshot.fields.get("operating_income" if concept == "ebit" else concept)
        if field is None or field.amount is None:
            continue
        if field.observed_on != snapshot.period_end or (
            field.basis != "instant" and field.period_start != snapshot.period_start
        ):
            issues[concept] = "Observation/duration differs from period; retained in snapshot only"
            continue
        amount = field.amount
        if concept == "ebit":
            amount = Sourced[float](
                value=amount.value,
                prov=amount.prov.model_copy(
                    update={
                        "note": (amount.prov.note or "")
                        + "; Raw EBIT uses operating profit, excluding nonoperating income; "
                        "not normalized"
                    }
                ),
            )
        values[concept] = amount

    sections: dict[Section, tuple[EvidenceCell, ...]] = {}
    for section in ("income_statement", "balance_sheet", "cash_flow"):
        try:
            sections[section] = read_statement(tools, section)
        except SECError as error:
            warnings.append(str(error))
            sections[section] = ()

    # Companyfacts includes standard footnote disclosures. Pin to this filing,
    # not just its fiscal end, so a later restatement cannot leak into enrichment.
    tools.audit.append("read_filing_xbrl_disclosures")
    try:
        disclosure_facts = tuple(
            f
            for f in tools.client.get_company_facts(tools.cik, tools.as_of)
            if f.accession == tools.filing.accession
            and f.filed == tools.filing.filed
            and f.namespace == "us-gaap"
        )
    except SECError as error:
        disclosure_facts = ()
        warnings.append("Filing XBRL disclosures unavailable: " + str(error))

    def eligible(section: Section, names: tuple[str, ...]) -> list[EvidenceCell]:
        cells = [
            c
            for c in sections[section]
            if c.tag in {"us-gaap:" + name for name in names}
            and c.unit == "USD"
            and c.period_end == snapshot.period_end
            and c.filed <= snapshot.as_of
            and c.period_start == (None if section == "balance_sheet" else snapshot.period_start)
        ]
        if cells:
            return cells  # Never replace a conflicting primary selection with a fallback.
        found: dict[tuple[str, float], EvidenceCell] = {}
        for fact in disclosure_facts:
            if (
                fact.tag not in names
                or fact.unit != "USD"
                or fact.end != snapshot.period_end
                or fact.start != (None if section == "balance_sheet" else snapshot.period_start)
            ):
                continue
            found[(fact.tag, fact.value)] = EvidenceCell(
                cell_id="X:" + fact.tag,
                label="Filing XBRL disclosure: " + fact.tag,
                tag="us-gaap:" + fact.tag,
                value=fact.value,
                unit="USD",
                period_start=fact.start,
                period_end=fact.end,
                filed=fact.filed,
                accession=fact.accession,
                ref=fact.sourced(tools.cik, tools.as_of).prov.ref,
                source="sec_xbrl",
                section=section,
            )
        return list(found.values())

    def accept(concept: FinancialConcept, cells: list[EvidenceCell]) -> None:
        if len({(c.accession, c.period_start, c.period_end) for c in cells}) != 1:
            raise SECError("Cannot combine periods or filings")
        if concept not in ("income_tax_expense", "pretax_income", "change_in_cash") and any(
            c.value < 0 for c in cells
        ):
            raise SECError("Negative magnitude requires review")
        warnings.extend(tools.reconcile(tuple(cells)))
        values[concept] = Sourced[float](
            value=sum(c.value for c in cells),
            prov=Provenance(
                source="derived" if len(cells) > 1 else cells[0].source,
                ref=" | ".join(c.ref for c in cells),
                as_of=snapshot.as_of,
                period_end=snapshot.period_end,
                filed=max(c.filed for c in cells),
                confidence="medium",
                note="Statement/disclosure extraction: "
                + concept
                + "; "
                + " | ".join(f"{c.tag}: {c.label}={c.value:g}" for c in cells),
            ),
        )

    for concept, (section, tags) in RULES.items():
        cells = eligible(section, tags)
        if len(cells) != 1:
            issues[concept] = "No unique supported total for this exact period"
            continue
        try:
            accept(concept, cells)
        except SECError as error:
            issues[concept] = str(error)

    for concept, tag in TOTAL_COMPONENTS.items():
        totals = eligible("balance_sheet", (tag,))
        current = eligible("balance_sheet", (tag + "Current",))
        noncurrent = eligible("balance_sheet", (tag + "Noncurrent",))
        if len(totals) == 1:
            cells = totals
            if (
                len(current) == len(noncurrent) == 1
                and abs(totals[0].value - current[0].value - noncurrent[0].value) > 1
            ):
                issues[concept] = "Total conflicts with current/noncurrent components"
                continue
        elif not totals and len(current) == len(noncurrent) == 1:
            cells = current + noncurrent
        else:
            issues[concept] = "Need a unique total or both current and noncurrent components"
            continue
        try:
            accept(concept, cells)
        except SECError as error:
            issues[concept] = str(error)

    for concept in get_args(FinancialConcept):
        if concept not in values:
            issues.setdefault(concept, "No supported cited value; not assumed zero")
    if not values:
        raise SECError("No validated financial values for the raw period")
    period = FinancialPeriod(
        kind=snapshot.kind,
        fiscal_year=snapshot.fiscal_year,
        fiscal_quarter=snapshot.fiscal_quarter,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        values=values,
    )
    return (
        RawFinancials(
            cik=snapshot.cik,
            as_of=snapshot.as_of,
            annual=(period,) if snapshot.kind == "annual" else (),
            quarterly=(period,) if snapshot.kind == "quarterly" else (),
        ),
        issues,
        tuple(warnings),
    )
