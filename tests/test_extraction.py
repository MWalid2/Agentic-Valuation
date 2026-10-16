"""General statement cases on synthetic packages: classification, statuses and derivations."""

import json
from datetime import date
from pathlib import Path

from test_llm import config as llm_config

from dcf_agent.agents.lines import (
    MappingStore,
    candidates,
    classify_unknown_lines,
    plausible,
)
from dcf_agent.data.extraction import extract
from dcf_agent.data.sec import Fact, Filing
from dcf_agent.data.xbrl import CalcArc, Element, Line, Statement, XBRLPackage
from dcf_agent.llm import LocalLLM

CUTOFF = date(2026, 10, 11)
END = date(2025, 12, 31)
START = date(2025, 1, 1)
FILING = Filing(
    accession="0000000001-26-000001",
    form="10-K",
    filed=date(2026, 2, 15),
    report_date=END,
    primary_document="ex.htm",
)
BS = "us-gaap:StatementOfFinancialPositionAbstract"


def fact(concept: str, value: float, start: date | None = None) -> Fact:
    prefix, tag = concept.split(":", 1)
    return Fact(
        namespace=prefix,
        tag=tag,
        unit="USD",
        value=value,
        start=start,
        end=END,
        filed=FILING.filed,
        accession=FILING.accession,
        form="10-K",
        fiscal_year=2025,
        fiscal_period="FY",
    )


def statement(kind, rows, calcs, title="Statement"):
    lines = tuple(Line(concept, label, 1, BS, False) for concept, label in rows)
    arcs = tuple(CalcArc(p, c, w) for p, c, w in calcs)
    return Statement(kind, "http://ex.com/role/" + kind, title, lines, arcs)


def balance_sheet(liability_rows, liability_values, extra_calcs=(), equity_rows=(), equity=(1000,)):
    """Assets 5,000 = liabilities (sum of liability rows) + equity rows."""
    rows = [("us-gaap:Cash", "Cash"), ("us-gaap:OtherAssetsNoncurrent", "Other assets")]
    rows += [("us-gaap:Assets", "Total assets")]
    rows += list(liability_rows) + [("us-gaap:Liabilities", "Total liabilities")]
    rows += [("us-gaap:StockholdersEquity", "Total equity")] + list(equity_rows)
    rows += [("us-gaap:LiabilitiesAndStockholdersEquity", "Total liabilities and equity")]
    liabilities = sum(liability_values)
    equity_total = 5000 - liabilities - sum(equity[1:])
    calcs = [
        ("us-gaap:Assets", "us-gaap:Cash", 1),
        ("us-gaap:Assets", "us-gaap:OtherAssetsNoncurrent", 1),
        ("us-gaap:LiabilitiesAndStockholdersEquity", "us-gaap:Liabilities", 1),
        ("us-gaap:LiabilitiesAndStockholdersEquity", "us-gaap:StockholdersEquity", 1),
    ]
    calcs += [("us-gaap:Liabilities", c, 1) for c, _ in liability_rows]
    calcs += [("us-gaap:LiabilitiesAndStockholdersEquity", c, 1) for c, _ in equity_rows]
    calcs += list(extra_calcs)
    facts = [
        fact("us-gaap:Cash", 1000),
        fact("us-gaap:OtherAssetsNoncurrent", 4000),
        fact("us-gaap:Assets", 5000),
        fact("us-gaap:Liabilities", liabilities),
        fact("us-gaap:StockholdersEquity", equity_total),
        fact("us-gaap:LiabilitiesAndStockholdersEquity", 5000),
    ]
    facts += [fact(c, v) for (c, _), v in zip(liability_rows, liability_values, strict=True)]
    facts += [fact(c, v) for (c, _), v in zip(equity_rows, equity[1:], strict=True)]
    return statement("balance_sheet", rows, calcs), facts


def package(statements, facts, *, elements=None, enumerations=None, dimensional=None):
    return XBRLPackage(
        cik="0000000001",
        filing=FILING,
        company_prefix="ex",
        fiscal_year=2025,
        fiscal_period="FY",
        facts=tuple(facts),
        decimals={},
        elements=elements or {},
        labels={},
        statements={s.kind: s for s in statements},
        dimensional=dimensional or {},
        enumerations=enumerations or {},
    )


def at(extraction, concept, period=(None, END)):
    return extraction.periods[period][concept]


def test_borrowings_add_only_as_issuer_components_and_flag_finance_leases():
    rows = [
        ("us-gaap:CommercialPaper", "Commercial paper"),
        ("us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent", "Current maturities of debt"),
        ("ex:TermLoanFacility", "Term loan facility"),
        ("us-gaap:AccountsPayableCurrent", "Accounts payable"),
    ]
    sheet, facts = balance_sheet(rows, [100, 200, 300, 50])
    result = extract(package([sheet], facts), CUTOFF)
    debt = at(result, "debt")
    assert debt.status == "reported" and debt.value.value == 600
    assert debt.basis == "includes finance lease liabilities"
    assert "ex:TermLoanFacility" in debt.elements
    assert at(result, "payables").value.value == 50


def test_debt_total_and_its_components_are_not_double_counted():
    rows = [("us-gaap:LongTermDebt", "Total debt")]
    extra = [
        ("us-gaap:LongTermDebt", "us-gaap:LongTermDebtCurrent", 1),
        ("us-gaap:LongTermDebt", "us-gaap:LongTermDebtNoncurrent", 1),
    ]
    sheet, facts = balance_sheet(rows, [900], extra)
    sheet = Statement(
        sheet.kind,
        sheet.role,
        sheet.title,
        sheet.lines
        + (
            Line("us-gaap:LongTermDebtCurrent", "Current", 2, "us-gaap:LongTermDebt", False),
            Line("us-gaap:LongTermDebtNoncurrent", "Noncurrent", 2, "us-gaap:LongTermDebt", False),
        ),
        sheet.calculations,
    )
    facts += [fact("us-gaap:LongTermDebtCurrent", 100), fact("us-gaap:LongTermDebtNoncurrent", 800)]
    assert at(extract(package([sheet], facts), CUTOFF), "debt").value.value == 900


def test_debt_free_company_needs_complete_statement_and_no_note_debt():
    sheet, facts = balance_sheet([("us-gaap:AccountsPayableCurrent", "Accounts payable")], [50])
    assert at(extract(package([sheet], facts), CUTOFF), "debt").status == "not_presented"
    noted = facts + [fact("us-gaap:LongTermDebt", 75)]
    debt = at(extract(package([sheet], noted), CUTOFF), "debt")
    assert debt.status == "unresolved" and "notes" in debt.reason
    broken = [f for f in facts if f.tag != "Cash"] + [fact("us-gaap:Cash", 900)]
    debt = at(extract(package([sheet], broken), CUTOFF), "debt")
    assert debt.status == "unresolved" and "incomplete" in debt.reason


def test_material_unknown_liability_blocks_debt_but_immaterial_one_does_not():
    rows = [("us-gaap:ShortTermBorrowings", "Borrowings"), ("ex:CustomerFinancing", "Financing")]
    sheet, facts = balance_sheet(rows, [100, 400])
    debt = at(extract(package([sheet], facts), CUTOFF), "debt")
    assert debt.status == "unresolved" and "ex:CustomerFinancing" in debt.reason
    sheet, facts = balance_sheet(rows, [100, 10])
    assert at(extract(package([sheet], facts), CUTOFF), "debt").value.value == 100


def test_lease_location_tag_classifies_extension_line_and_notes_supply_total():
    rows = [("ex:LeaseLiabilityCurrentNet", "Current lease liability")]
    sheet, facts = balance_sheet(rows, [40])
    tag = "us-gaap:OperatingLeaseLiabilityCurrentStatementOfFinancialPositionExtensibleList"
    pkg = package([sheet], facts, enumerations={(tag, None, END): ("ex:LeaseLiabilityCurrentNet",)})
    lease = at(extract(pkg, CUTOFF), "operating_lease_liabilities")
    assert lease.status == "reported" and lease.value.value == 40
    rows = [("us-gaap:AccruedLiabilitiesCurrent", "Accrued liabilities")]
    sheet, facts = balance_sheet(rows, [300])
    facts += [
        fact("us-gaap:OperatingLeaseLiabilityCurrent", 30),
        fact("us-gaap:OperatingLeaseLiabilityNoncurrent", 70),
    ]
    pkg = package(
        [sheet], facts, enumerations={(tag, None, END): ("us-gaap:AccruedLiabilitiesCurrent",)}
    )
    lease = at(extract(pkg, CUTOFF), "operating_lease_liabilities")
    assert lease.value.value == 100
    assert lease.basis == "from notes; included in Accrued liabilities"
    assert at(
        extract(package([sheet], facts[:-2]), CUTOFF), "operating_lease_liabilities"
    ).status == ("not_disclosed")


def test_total_liabilities_derived_from_issuer_calculation_when_not_presented():
    sheet, facts = balance_sheet([("us-gaap:AccountsPayableCurrent", "Payables")], [600])
    sheet = Statement(
        sheet.kind,
        sheet.role,
        sheet.title,
        tuple(line for line in sheet.lines if line.concept != "us-gaap:Liabilities"),
        tuple(
            CalcArc("us-gaap:LiabilitiesAndStockholdersEquity", a.child, a.weight)
            if a.parent == "us-gaap:Liabilities"
            else a
            for a in sheet.calculations
            if a.child != "us-gaap:Liabilities"
        ),
    )
    facts = [f for f in facts if f.tag != "Liabilities"]
    liabilities = at(extract(package([sheet], facts), CUTOFF), "total_liabilities")
    assert liabilities.status == "reported" and liabilities.value.value == 600


def test_redeemable_preferred_in_temporary_equity_is_a_claim():
    equity_rows = [
        (
            "us-gaap:TemporaryEquityCarryingAmountAttributableToParent",
            "Series A redeemable preferred",
        ),
        ("us-gaap:PreferredStockValue", "Preferred stock"),
    ]
    sheet, facts = balance_sheet([], [], equity_rows=equity_rows, equity=(0, 195, 0))
    result = extract(package([sheet], facts), CUTOFF)
    preferred = at(result, "preferred_equity")
    assert preferred.value.value == 195
    assert "temporary equity" in preferred.basis
    assert at(result, "minority_interest").status == "not_presented"


def test_exact_cash_line_beats_broader_subtotal_and_investments_go_to_securities():
    rows = [
        ("us-gaap:CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents"),
        ("us-gaap:ShortTermInvestments", "Short-term investments"),
        ("us-gaap:CashCashEquivalentsAndShortTermInvestments", "Total cash and investments"),
        ("us-gaap:Assets", "Total assets"),
        ("us-gaap:Liabilities", "Total liabilities"),
        ("us-gaap:StockholdersEquity", "Equity"),
        ("us-gaap:LiabilitiesAndStockholdersEquity", "Total"),
    ]
    calcs = [
        ("us-gaap:CashCashEquivalentsAndShortTermInvestments", rows[0][0], 1),
        ("us-gaap:CashCashEquivalentsAndShortTermInvestments", rows[1][0], 1),
        ("us-gaap:Assets", "us-gaap:CashCashEquivalentsAndShortTermInvestments", 1),
        ("us-gaap:LiabilitiesAndStockholdersEquity", "us-gaap:Liabilities", 1),
        ("us-gaap:LiabilitiesAndStockholdersEquity", "us-gaap:StockholdersEquity", 1),
    ]
    facts = [
        fact(rows[0][0], 60),
        fact(rows[1][0], 40),
        fact(rows[2][0], 100),
        fact("us-gaap:Assets", 100),
        fact("us-gaap:Liabilities", 30),
        fact("us-gaap:StockholdersEquity", 70),
        fact("us-gaap:LiabilitiesAndStockholdersEquity", 100),
    ]
    result = extract(package([statement("balance_sheet", rows, calcs)], facts), CUTOFF)
    assert at(result, "cash").value.value == 60 and at(result, "cash").basis is None
    assert at(result, "marketable_securities").value.value == 40


def income(rows, calcs, values):
    facts = [fact(c, v, START) for c, v in values.items()]
    return statement("income_statement", rows, calcs), facts


def test_pretax_found_by_calculation_position_and_ebit_derived_without_operating_line():
    # An issuer tags its pretax line with a broader element and shows no operating income.
    rows = [
        ("us-gaap:Revenues", "Revenues"),
        ("us-gaap:CostOfRevenue", "Cost of sales"),
        ("us-gaap:SellingGeneralAndAdministrativeExpense", "SG&A"),
        ("us-gaap:InterestIncomeExpenseNonoperatingNet", "Interest (income) expense, net"),
        ("us-gaap:IncomeLossAttributableToParent", "Earnings before income taxes"),
        ("us-gaap:IncomeTaxExpenseBenefit", "Income taxes"),
        ("us-gaap:NetIncomeLoss", "Net earnings"),
        ("us-gaap:EarningsPerShareBasic", "EPS"),
    ]
    pretax = "us-gaap:IncomeLossAttributableToParent"
    calcs = [
        (pretax, "us-gaap:Revenues", 1),
        (pretax, "us-gaap:CostOfRevenue", -1),
        (pretax, "us-gaap:SellingGeneralAndAdministrativeExpense", -1),
        (pretax, "us-gaap:InterestIncomeExpenseNonoperatingNet", 1),
        ("us-gaap:NetIncomeLoss", pretax, 1),
        ("us-gaap:NetIncomeLoss", "us-gaap:IncomeTaxExpenseBenefit", -1),
    ]
    values = {
        "us-gaap:Revenues": 1000,
        "us-gaap:CostOfRevenue": 600,
        "us-gaap:SellingGeneralAndAdministrativeExpense": 200,
        "us-gaap:InterestIncomeExpenseNonoperatingNet": -20,
        pretax: 180,
        "us-gaap:IncomeTaxExpenseBenefit": 40,
        "us-gaap:NetIncomeLoss": 140,
    }
    sheet, facts = income(rows, calcs, values)
    result = extract(package([sheet], facts), CUTOFF)
    period = (START, END)
    assert at(result, "pretax_income", period).value.value == 180
    ebit = at(result, "ebit", period)
    assert ebit.value.value == 200 and ebit.basis.startswith("derived")
    interest = at(result, "interest_expense", period)
    assert interest.value.value == 20 and interest.basis == "net of interest income"


def test_pretax_equals_net_income_without_any_tax_line_and_interest_can_be_undisclosed():
    rows = [
        ("us-gaap:Revenues", "Revenue"),
        ("us-gaap:ResearchAndDevelopmentExpense", "R&D"),
        ("us-gaap:NetIncomeLoss", "Net loss"),
        ("us-gaap:EarningsPerShareBasic", "EPS"),
    ]
    calcs = [
        ("us-gaap:NetIncomeLoss", "us-gaap:Revenues", 1),
        ("us-gaap:NetIncomeLoss", "us-gaap:ResearchAndDevelopmentExpense", -1),
    ]
    sheet, facts = income(
        rows,
        calcs,
        {
            "us-gaap:Revenues": 10,
            "us-gaap:ResearchAndDevelopmentExpense": 50,
            "us-gaap:NetIncomeLoss": -40,
        },
    )
    result = extract(package([sheet], facts), CUTOFF)
    period = (START, END)
    assert at(result, "income_tax_expense", period).status == "not_presented"
    pretax = at(result, "pretax_income", period)
    assert pretax.value.value == -40 and "no income tax" in pretax.basis
    assert at(result, "interest_expense", period).status == "not_disclosed"


def test_negative_standard_magnitude_is_unresolved_but_extension_sign_is_normalized():
    rows = [
        ("us-gaap:NetCashProvidedByUsedInOperatingActivities", "Operating"),
        ("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", "Capital expenditures"),
        ("us-gaap:NetCashProvidedByUsedInInvestingActivities", "Investing"),
    ]
    calcs = [
        (
            "us-gaap:NetCashProvidedByUsedInInvestingActivities",
            "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
            -1,
        )
    ]
    values = {
        "us-gaap:NetCashProvidedByUsedInOperatingActivities": 500,
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment": -80,
        "us-gaap:NetCashProvidedByUsedInInvestingActivities": 80,
    }
    sheet = statement("cash_flow", rows, calcs)
    facts = [fact(c, v, START) for c, v in values.items()]
    assert at(extract(package([sheet], facts), CUTOFF), "capex", (START, END)).status == (
        "unresolved"
    )
    rows[1] = ("ex:PurchasesOfPropertyAndEquipment", "Purchases of property and equipment")
    values["ex:PurchasesOfPropertyAndEquipment"] = values.pop(
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"
    )
    calcs = [("us-gaap:NetCashProvidedByUsedInInvestingActivities", rows[1][0], 1)]
    sheet = statement("cash_flow", rows, calcs)
    facts = [fact(c, v, START) for c, v in values.items()]
    capex = at(extract(package([sheet], facts), CUTOFF), "capex", (START, END))
    assert capex.value.value == 80


def test_model_classifies_unknown_material_line_once_and_decision_is_reused(tmp_path: Path):
    rows = [
        ("us-gaap:ShortTermBorrowings", "Borrowings"),
        ("ex:RoyaltyObligation", "Royalty obligation"),
    ]
    sheet, facts = balance_sheet(rows, [100, 400])
    pkg = package(
        [sheet], facts, elements={"ex:RoyaltyObligation": Element("ex:RoyaltyObligation")}
    )
    first = extract(pkg, CUTOFF)
    assert at(first, "debt").status == "unresolved"
    store = MappingStore(tmp_path / "mappings")
    reply = json.dumps({"decisions": [{"line_id": "L1", "category": "borrowing"}]})
    with LocalLLM(llm_config(tmp_path), mock_responses=[reply]) as llm:
        learned = classify_unknown_lines(llm, first, store)
    assert learned == {"ex:RoyaltyObligation": "borrowing"}
    assert store.load(pkg.cik) == learned
    debt = at(extract(pkg, CUTOFF, learned), "debt")
    assert debt.status == "reported" and debt.value.value == 500
    # Stored decisions are reused: no further model response is available or needed.
    with LocalLLM(llm_config(tmp_path / "second")) as llm:
        assert classify_unknown_lines(llm, first, store) == learned


def test_model_choice_outside_the_allowed_list_is_ignored(tmp_path: Path):
    rows = [("ex:RoyaltyObligation", "Royalty obligation")]
    sheet, facts = balance_sheet(rows, [400])
    first = extract(package([sheet], facts), CUTOFF)
    reply = json.dumps({"decisions": [{"line_id": "L1", "category": "revenue"}]})
    with LocalLLM(llm_config(tmp_path), mock_responses=[reply]) as llm:
        assert classify_unknown_lines(llm, first, MappingStore(tmp_path / "m")) == {}


def test_implausible_model_choice_is_rejected_by_label_and_section(tmp_path: Path):
    rows = [
        ("us-gaap:NetCashProvidedByUsedInOperatingActivities", "Operating"),
        ("ex:PurchasesOfInvestments", "Purchases of investments"),
        ("us-gaap:NetCashProvidedByUsedInInvestingActivities", "Investing"),
    ]
    calcs = [("us-gaap:NetCashProvidedByUsedInInvestingActivities", rows[1][0], 1)]
    sheet = statement("cash_flow", rows, calcs)
    values = {rows[0][0]: 500, rows[1][0]: -300, rows[2][0]: -300}
    facts = [fact(c, v, START) for c, v in values.items()]
    assets = [fact("us-gaap:Assets", 1000)]
    first = extract(package([sheet], facts + assets), CUTOFF)
    line = next(c for c in first.classifications["cash_flow"] if c.category == "unknown")
    assert not plausible(first, "cash_flow", line, "capex")
    assert plausible(first, "cash_flow", line, "other")
    # It blocks change in cash (no label hint), so the model is asked; "capex" is refused.
    assert [c.line.concept for _, c, _ in candidates(first)] == ["ex:PurchasesOfInvestments"]
    reply = json.dumps({"decisions": [{"line_id": "L1", "category": "capex"}]})
    with LocalLLM(llm_config(tmp_path), mock_responses=[reply]) as llm:
        assert classify_unknown_lines(llm, first, MappingStore(tmp_path / "m")) == {}


def test_distributions_to_noncontrolling_holders_are_not_dividends(tmp_path: Path):
    rows = [
        ("us-gaap:NetCashProvidedByUsedInOperatingActivities", "Operating"),
        ("ex:DistributionsToNci", "Capital distributions paid to non-controlling interests"),
        ("us-gaap:NetCashProvidedByUsedInFinancingActivities", "Financing"),
    ]
    calcs = [("us-gaap:NetCashProvidedByUsedInFinancingActivities", rows[1][0], 1)]
    values = {rows[0][0]: 500, rows[1][0]: -50, rows[2][0]: -50}
    sheet = statement("cash_flow", rows, calcs)
    first = extract(package([sheet], [fact(c, v, START) for c, v in values.items()]), CUTOFF)
    line = next(c for c in first.classifications["cash_flow"] if c.category == "unknown")
    assert not plausible(first, "cash_flow", line, "dividends")


def test_single_step_statement_opens_cost_subtotal_containing_interest():
    total = "us-gaap:CostsAndExpenses"
    pretax = (
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
        "ExtraordinaryItemsNoncontrollingInterest"
    )
    rows = [
        ("us-gaap:Revenues", "Revenues"),
        ("us-gaap:CostOfRevenue", "Production costs"),
        ("ex:SeparationCosts", "Business separation costs"),
        ("us-gaap:InterestExpense", "Interest expense"),
        (total, "Total costs and other deductions"),
        (pretax, "Income before income taxes"),
        ("us-gaap:IncomeTaxExpenseBenefit", "Income taxes"),
        ("us-gaap:NetIncomeLoss", "Net income"),
        ("us-gaap:EarningsPerShareBasic", "EPS"),
    ]
    calcs = [
        (pretax, "us-gaap:Revenues", 1),
        (pretax, total, -1),
        (total, "us-gaap:CostOfRevenue", 1),
        (total, "ex:SeparationCosts", 1),
        (total, "us-gaap:InterestExpense", 1),
        ("us-gaap:NetIncomeLoss", pretax, 1),
        ("us-gaap:NetIncomeLoss", "us-gaap:IncomeTaxExpenseBenefit", -1),
    ]
    values = {
        "us-gaap:Revenues": 1000,
        "us-gaap:CostOfRevenue": 700,
        "ex:SeparationCosts": 20,
        "us-gaap:InterestExpense": 30,
        total: 750,
        pretax: 250,
        "us-gaap:IncomeTaxExpenseBenefit": 50,
        "us-gaap:NetIncomeLoss": 200,
    }
    sheet, facts = income(rows, calcs, values)
    ebit = at(extract(package([sheet], facts), CUTOFF), "ebit", (START, END))
    assert ebit.value.value == 280  # 1000 - 700 - 20; interest removed from the subtotal.
    assert "Business separation costs" in ebit.basis


def test_note_only_dates_are_not_statement_periods():
    sheet, facts = balance_sheet([("us-gaap:AccountsPayableCurrent", "Payables")], [50])
    # A segment note repeats total assets for an earlier date; the statement does not.
    older = date(2023, 12, 31)
    facts.append(fact("us-gaap:Assets", 4000).model_copy(update={"end": older}))
    result = extract(package([sheet], facts), CUTOFF)
    assert (None, END) in result.periods and (None, older) not in result.periods


def test_related_party_split_sums_members_of_the_presented_axis_only():
    axis = "us-gaap:RelatedPartyTransactionsByRelatedPartyAxis"
    rows = [
        ("us-gaap:AccountsReceivableNetCurrent", "Accounts receivable, net"),
        (axis, "Related party [Axis]"),
        ("us-gaap:NonrelatedPartyMember", "Nonrelated party [Member]"),
        ("us-gaap:RelatedPartyMember", "Related party [Member]"),
        ("us-gaap:AccountsPayableCurrent", "Payables"),
    ]
    sheet, facts = balance_sheet(rows[4:], [50])
    sheet = Statement(
        sheet.kind,
        sheet.role,
        sheet.title,
        sheet.lines + tuple(Line(c, label, 1, BS, i > 0) for i, (c, label) in enumerate(rows[:4])),
        sheet.calculations,
    )
    concept = "us-gaap:AccountsReceivableNetCurrent"
    dimensional = {
        (concept, None, END, "USD", ((axis, "us-gaap:NonrelatedPartyMember"),)): 300.0,
        (concept, None, END, "USD", ((axis, "us-gaap:RelatedPartyMember"),)): 20.0,
        # A note-level breakdown on two axes must not be added again.
        (
            concept,
            None,
            END,
            "USD",
            (
                ("srt:CounterpartyNameAxis", "ex:AffiliateMember"),
                (axis, "us-gaap:RelatedPartyMember"),
            ),
        ): 20.0,
    }
    result = extract(package([sheet], facts, dimensional=dimensional), CUTOFF)
    assert at(result, "receivables").value.value == 320


def test_pre_revenue_company_is_a_scope_block_not_a_data_failure():
    from dcf_agent.contracts.inputs import ConceptStatus, FinancialPeriod, RawFinancials
    from dcf_agent.data.readiness import assess_readiness

    period = FinancialPeriod(
        kind="annual",
        fiscal_year=2025,
        period_start=START,
        period_end=END,
        values={"net_income": fact("us-gaap:NetIncomeLoss", -5, START).sourced("1", CUTOFF)},
        statuses={"revenue": ConceptStatus(status="not_presented", reason="No revenue line")},
    )
    raw = RawFinancials(cik="0000000001", as_of=CUTOFF, annual=(period,))
    assert "scope" in assess_readiness(raw, {})["normalization"].missing


SEGMENT = "srt:ProductOrServiceAxis"
INDUSTRIAL = "ex:MachineryMember"
FINANCE = "ex:FinancialProductsMember"


def captive_sheet(split: bool, overlapping: bool = False):
    rows = [("us-gaap:LongTermDebtNoncurrent", "Long-term debt")]
    sheet, facts = balance_sheet(rows, [0])
    presented = [(SEGMENT, "Segment [Axis]"), (INDUSTRIAL, "Machinery"), (FINANCE, "Financial")]
    sheet = Statement(
        sheet.kind,
        sheet.role,
        sheet.title,
        sheet.lines + tuple(Line(c, label, 1, BS, True) for c, label in presented),
        sheet.calculations,
    )
    # Consolidated debt (1,000) appears only by member: 300 industrial + 700 finance arm.
    facts = [f for f in facts if f.tag != "LongTermDebtNoncurrent"]
    key = ("us-gaap:LongTermDebtNoncurrent", None, END, "USD")
    dimensional = {}
    if split:
        dimensional[(*key, ((SEGMENT, INDUSTRIAL),))] = 300.0
        dimensional[(*key, ((SEGMENT, FINANCE),))] = 700.0
        if overlapping:
            # The same column tagged again with a segment member not on the statement.
            dimensional[(*key, ((SEGMENT, "ex:FinancialProductsSegmentMember"),))] = 700.0
    else:
        facts.append(fact("us-gaap:LongTermDebtNoncurrent", 1000))
        dimensional[
            (
                "us-gaap:Revenues",
                START,
                END,
                "USD",
                (("us-gaap:StatementBusinessSegmentsAxis", FINANCE),),
            )
        ] = 50.0
    facts = [f for f in facts if f.tag not in ("Liabilities",)] + [
        fact("us-gaap:Liabilities", 1000)
    ]
    facts = [f for f in facts if f.tag != "StockholdersEquity"] + [
        fact("us-gaap:StockholdersEquity", 4000)
    ]
    return package([sheet], facts, dimensional=dimensional)


def test_captive_finance_debt_is_separated_when_the_statement_splits_it():
    result = extract(captive_sheet(split=True, overlapping=True), CUTOFF)
    debt, captive = at(result, "debt"), at(result, "captive_finance_debt")
    assert captive.value.value == 700  # Overlapping unpresented member not added twice.
    assert debt.value.value == 300 and "excludes captive-finance debt" in debt.basis


def test_captive_finance_without_a_split_is_unresolved():
    result = extract(captive_sheet(split=False), CUTOFF)
    assert at(result, "debt").value.value == 1000
    captive = at(result, "captive_finance_debt")
    assert captive.status == "unresolved" and "not split" in captive.reason


def test_spac_redeemable_shares_are_a_claim_but_temporary_preferred_is_not_double_counted():
    temp = "us-gaap:TemporaryEquityCarryingAmountAttributableToParent"
    equity_rows = [(temp, "Class A shares subject to possible redemption")]
    sheet, facts = balance_sheet([], [], equity_rows=equity_rows, equity=(0, 900))
    result = extract(package([sheet], facts), CUTOFF)
    claim = at(result, "temporary_equity")
    assert claim.status == "reported" and claim.value.value == 900
    assert at(result, "preferred_equity").status == "not_presented"


def test_custom_lease_line_matching_the_lease_disclosure_is_a_lease_and_liabilities_derive():
    rows = [("ex:LongTermLeaseLiabilityNet", "Long-term lease liability, net")]
    sheet, facts = balance_sheet(rows, [140])
    facts.append(fact("us-gaap:OperatingLeaseLiabilityNoncurrent", 140))
    # No total liabilities line: liabilities come from the issuer's calculation by side.
    sheet = Statement(
        sheet.kind,
        sheet.role,
        sheet.title,
        tuple(line for line in sheet.lines if line.concept != "us-gaap:Liabilities"),
        tuple(
            CalcArc("us-gaap:LiabilitiesAndStockholdersEquity", a.child, a.weight)
            if a.parent == "us-gaap:Liabilities"
            else a
            for a in sheet.calculations
            if a.child != "us-gaap:Liabilities"
        ),
    )
    facts = [f for f in facts if f.tag != "Liabilities"]
    result = extract(package([sheet], facts), CUTOFF)
    lease = at(result, "operating_lease_liabilities")
    assert lease.status == "reported" and lease.value.value == 140
    assert at(result, "total_liabilities").value.value == 140


def profit_loss_statement(minority: bool):
    """A pre-revenue company whose bottom line is tagged only as consolidated ProfitLoss."""
    rows = [
        ("us-gaap:ResearchAndDevelopmentExpense", "Research and development"),
        ("us-gaap:OperatingIncomeLoss", "Loss from operations"),
        ("us-gaap:InvestmentIncomeInterest", "Interest income"),
        ("us-gaap:ProfitLoss", "Net loss"),
        ("us-gaap:EarningsPerShareBasic", "EPS"),
    ]
    values = {
        "us-gaap:ResearchAndDevelopmentExpense": 50,
        "us-gaap:OperatingIncomeLoss": -50,
        "us-gaap:InvestmentIncomeInterest": 5,
        "us-gaap:ProfitLoss": -45,
    }
    if minority:
        rows.append(("us-gaap:NetIncomeLossAttributableToNoncontrollingInterest", "Minority"))
        values["us-gaap:NetIncomeLossAttributableToNoncontrollingInterest"] = -5
    calcs = [
        ("us-gaap:OperatingIncomeLoss", "us-gaap:ResearchAndDevelopmentExpense", -1),
        ("us-gaap:ProfitLoss", "us-gaap:OperatingIncomeLoss", 1),
        ("us-gaap:ProfitLoss", "us-gaap:InvestmentIncomeInterest", 1),
    ]
    sheet, facts = income(rows, calcs, values)
    return extract(package([sheet], facts), CUTOFF)


def test_consolidated_profit_anchors_the_period_and_is_net_income_without_minority():
    result = profit_loss_statement(minority=False)
    period = (START, END)
    assert at(result, "revenue", period).status == "not_presented"
    assert at(result, "ebit", period).value.value == -50
    net = at(result, "net_income", period)
    assert net.status == "reported" and net.value.value == -45
    assert "no noncontrolling interest" in (net.value.prov.note or "")


def test_consolidated_profit_is_not_parent_net_income_when_minority_is_presented():
    result = profit_loss_statement(minority=True)
    assert at(result, "net_income", (START, END)).status != "reported"


def test_label_guards_use_word_boundaries():
    import re

    from dcf_agent.agents.lines import IMPLAUSIBLE, PLAUSIBLE

    assert re.search(PLAUSIBLE["cash"], "cash and cash equivalents held by subsidiaries")
    assert not re.search(PLAUSIBLE["cash"], "cashless exercise of warrants")
    # "Cost of sales" names sales but is a cost: never accepted as revenue.
    assert re.search(IMPLAUSIBLE["revenue"], "cost of sales")
    assert not re.search(IMPLAUSIBLE["revenue"], "net sales to customers")


def test_no_control_characters_in_source():
    """Shell-edited regexes once held backspace characters instead of word boundaries."""
    root = Path(__file__).resolve().parents[1]
    bad = []
    for path in [
        *root.glob("src/**/*.py"),
        *root.glob("scripts/**/*.py"),
        *root.glob("tests/*.py"),
    ]:
        text = path.read_text(encoding="utf-8")
        if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in text):
            bad.append(str(path.relative_to(root)))
    assert bad == []
