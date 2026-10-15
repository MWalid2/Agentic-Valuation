"""Classify primary-statement lines into valuation categories, for any issuer.

Standard elements use curated US-GAAP families and the taxonomy's systematic
naming. Extension elements use their calculation parent, balance type and
label. Lines the rules cannot place are left ``unknown`` for a bounded model
decision; nothing here names an issuer, ticker or expected amount.
"""

import re
from dataclasses import dataclass
from typing import Literal

from dcf_agent.data.xbrl import Line, Statement, StatementKind, XBRLPackage

Category = Literal[
    # Balance sheet
    "cash",
    "marketable_securities",
    "receivables",
    "inventory",
    "net_ppe",
    "goodwill",
    "total_assets",
    "payables",
    "borrowing",
    "borrowing_with_finance_lease",
    "operating_lease",
    "finance_lease",
    "total_liabilities",
    "equity",
    "equity_including_nci",
    "minority_interest",
    "temporary_equity",
    "preferred_equity",
    "liabilities_and_equity",
    "other_asset",
    "other_liability",
    "other_equity",
    # Income statement
    "revenue",
    "cogs",
    "sga",
    "rnd",
    "operating_income",
    "interest_expense",
    "interest_net",
    "pretax_income",
    "income_tax",
    "net_income",
    "net_income_common",
    # Cash flow
    "operating_cash_flow",
    "depreciation_amortization",
    "stock_based_compensation",
    "capex",
    "buybacks",
    "dividends",
    "change_in_cash",
    # Any statement
    "other",
    "unknown",
]
Side = Literal["asset", "liability", "equity", "flow"]


@dataclass(frozen=True)
class Classified:
    line: Line
    category: Category
    side: Side
    source: Literal["taxonomy", "pattern", "label", "model", "default"]
    basis: str | None = None


# Standard US-GAAP elements: (category, basis note when broader/narrower than the concept).
STANDARD: dict[str, tuple[Category, str | None]] = {}


def _family(category: Category, names: str, basis: str | None = None) -> None:
    for name in names.split():
        STANDARD[name] = (category, basis)


_family("cash", "CashAndCashEquivalentsAtCarryingValue Cash CashAndDueFromBanks")
_family(
    "cash",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "includes restricted cash",
)
_family(
    "cash",
    "CashCashEquivalentsAndShortTermInvestments",
    "includes short-term investments",
)
_family(
    "marketable_securities",
    "MarketableSecuritiesCurrent MarketableSecuritiesNoncurrent MarketableSecurities "
    "ShortTermInvestments OtherShortTermInvestments AvailableForSaleSecuritiesDebtSecuritiesCurrent "  # noqa: E501
    "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent AvailableForSaleSecuritiesDebtSecurities "
    "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent "
    "DebtSecuritiesAvailableForSaleExcludingAccruedInterestNoncurrent DebtSecuritiesCurrent "
    "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLossCurrent "
    "HeldToMaturitySecuritiesCurrent TradingSecuritiesCurrent EquitySecuritiesFvNiCurrent",
)
_family(
    "receivables",
    "AccountsReceivableNetCurrent ReceivablesNetCurrent AccountsNotesAndLoansReceivableNetCurrent "
    "AccountsReceivableNet AccountsReceivableGrossCurrent",
)
_family(
    "receivables",
    "AccountsAndOtherReceivablesNetCurrent",
    "includes other receivables",
)
_family(
    "inventory",
    "InventoryNet InventoryGross PublicUtilitiesInventory AirlineRelatedInventoryNet "
    "RetailRelatedInventoryMerchandise InventoryFinishedGoodsNetOfReserves "
    "InventoryRawMaterialsAndSuppliesNetOfReserves InventoryWorkInProcessNetOfReserves "
    "InventoryNetOfAllowancesCustomerAdvancesAndProgressBillings EnergyRelatedInventory "
    "InventorySuppliesNetOfReserves InventoryFinishedGoods InventoryRawMaterials "
    "InventoryWorkInProcess",
)
_family(
    "net_ppe",
    "PropertyPlantAndEquipmentNet PublicUtilitiesPropertyPlantAndEquipmentNet",
)
_family(
    "net_ppe",
    "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",  # noqa: E501
    "includes finance-lease right-of-use assets",
)
_family("goodwill", "Goodwill")
_family("total_assets", "Assets")
_family("payables", "AccountsPayableCurrent AccountsPayableTradeCurrent")
_family(
    "payables",
    "AccountsPayableAndAccruedLiabilitiesCurrent AccountsPayableAndOtherAccruedLiabilitiesCurrent",
    "includes accrued liabilities",
)
_family(
    "borrowing",
    "CommercialPaper ShortTermBorrowings DebtCurrent LongTermDebtCurrent LongTermDebtNoncurrent "
    "LongTermDebt NotesPayableCurrent NotesPayable NotesAndLoansPayable LoansPayableCurrent "
    "LongTermLoansPayable LongTermNotesPayable LongTermNotesAndLoans ConvertibleDebtNoncurrent "
    "ConvertibleDebtCurrent ConvertibleNotesPayableCurrent ConvertibleLongTermNotesPayable "
    "SecuredDebt SecuredDebtCurrent SecuredLongTermDebt UnsecuredDebt UnsecuredLongTermDebt "
    "SeniorLongTermNotes SeniorNotes ShortTermNonBankLoansAndNotesPayable "
    "ShortTermBankLoansAndNotesPayable OtherShortTermBorrowings LinesOfCreditCurrent "
    "LongTermLineOfCredit LineOfCredit OtherLongTermDebtNoncurrent OtherLongTermDebtCurrent "
    "OtherLongTermDebt DebtInstrumentCarryingAmount BankOverdrafts NotesPayableRelatedPartiesClassifiedCurrent "  # noqa: E501
    "NotesPayableRelatedPartiesNoncurrent DebtLongtermAndShorttermCombinedAmount",
)
_family(
    "borrowing_with_finance_lease",
    "LongTermDebtAndCapitalLeaseObligations LongTermDebtAndCapitalLeaseObligationsCurrent "
    "DebtAndCapitalLeaseObligations "
    "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities "
    "OtherLongTermDebtAndCapitalLeaseObligationsNoncurrent",
    "includes finance lease liabilities",
)
_family(
    "operating_lease",
    "OperatingLeaseLiability OperatingLeaseLiabilityCurrent OperatingLeaseLiabilityNoncurrent",
)
_family(
    "finance_lease",
    "FinanceLeaseLiability FinanceLeaseLiabilityCurrent FinanceLeaseLiabilityNoncurrent "
    "CapitalLeaseObligations CapitalLeaseObligationsCurrent CapitalLeaseObligationsNoncurrent",
)
_family("total_liabilities", "Liabilities")
_family("equity", "StockholdersEquity MembersEquity PartnersCapital")
_family(
    "equity_including_nci",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest "
    "LimitedLiabilityCompanyLlcMembersEquityIncludingPortionAttributableToNoncontrollingInterest "
    "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
)
_family(
    "minority_interest",
    "MinorityInterest NonredeemableNoncontrollingInterest OtherMinorityInterests "
    "MembersEquityAttributableToNoncontrollingInterest",
)
_family(
    "minority_interest",
    "RedeemableNoncontrollingInterestEquityCarryingAmount "
    "RedeemableNoncontrollingInterestEquityFairValue",
    "redeemable noncontrolling interest in temporary equity",
)
_family(
    "temporary_equity",
    "TemporaryEquityCarryingAmountAttributableToParent TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",  # noqa: E501
)
_family(
    "preferred_equity",
    "PreferredStockValue PreferredStockValueOutstanding PreferredStockIncludingAdditionalPaidInCapital "  # noqa: E501
    "PreferredStockIncludingAdditionalPaidInCapitalNetOfDiscount",
)
_family(
    "preferred_equity",
    "TemporaryEquityRedemptionValue RedeemablePreferredStockCarryingAmount "
    "RedeemableConvertiblePreferredStockCarryingAmount ConvertiblePreferredStockNonredeemableOrRedeemableIssuerOptionValue",  # noqa: E501
    "redeemable preferred stock in temporary equity",
)
_family("liabilities_and_equity", "LiabilitiesAndStockholdersEquity")
_family(
    "revenue",
    "Revenues RevenueFromContractWithCustomerExcludingAssessedTax "
    "RevenueFromContractWithCustomerIncludingAssessedTax SalesRevenueNet "
    "RegulatedAndUnregulatedOperatingRevenue RevenuesNetOfInterestExpense",
)
_family(
    "cogs",
    "CostOfRevenue CostOfGoodsAndServicesSold CostOfGoodsSold CostOfServices "
    "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
)
_family(
    "sga",
    "SellingGeneralAndAdministrativeExpense SellingAndMarketingExpense GeneralAndAdministrativeExpense",  # noqa: E501
)
_family(
    "rnd",
    "ResearchAndDevelopmentExpense ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
)
_family("operating_income", "OperatingIncomeLoss")
_family(
    "interest_expense",
    "InterestExpense InterestExpenseNonoperating InterestExpenseDebt InterestAndDebtExpense "
    "InterestExpenseOperating FinancingInterestExpense",
)
_family(
    "interest_net",
    "InterestIncomeExpenseNonoperatingNet InterestIncomeExpenseNet InterestIncomeExpenseAfterProvisionForLoanLoss",  # noqa: E501
    "net of interest income",
)
_family(
    "pretax_income",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest "
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments "  # noqa: E501
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
)
_family("income_tax", "IncomeTaxExpenseBenefit")
_family("net_income", "NetIncomeLoss")
_family(
    "net_income_common",
    "NetIncomeLossAvailableToCommonStockholdersBasic",
    "net income available to common stockholders",
)
_family("operating_cash_flow", "NetCashProvidedByUsedInOperatingActivities")
_family(
    "operating_cash_flow",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    "continuing operations only",
)
_family(
    "depreciation_amortization",
    "DepreciationDepletionAndAmortization DepreciationAndAmortization Depreciation "
    "AmortizationOfIntangibleAssets DepreciationAmortizationAndOther "
    "CostDepreciationAmortizationAndDepletion DepreciationNonproduction OtherDepreciationAndAmortization",  # noqa: E501
)
_family(
    "depreciation_amortization",
    "DepreciationAmortizationAndAccretionNet",
    "includes accretion",
)
_family("stock_based_compensation", "ShareBasedCompensation AllocatedShareBasedCompensationExpense")
_family(
    "capex",
    "PaymentsToAcquirePropertyPlantAndEquipment PaymentsToAcquireProductiveAssets "
    "PaymentsForCapitalImprovements PaymentsToAcquireOtherPropertyPlantAndEquipment "
    "PaymentsToAcquireMachineryAndEquipment PaymentsForConstructionInProcess",
)
_family("buybacks", "PaymentsForRepurchaseOfCommonStock PaymentsForRepurchaseOfEquity")
_family(
    "dividends",
    "PaymentsOfDividendsCommonStock PaymentsOfDividends PaymentsOfOrdinaryDividends",
)
_family(
    "change_in_cash",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect "  # noqa: E501
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",  # noqa: E501
    "includes restricted cash",
)
_family(
    "change_in_cash",
    "CashAndCashEquivalentsPeriodIncreaseDecrease CashAndCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect",  # noqa: E501
)

# Systematic taxonomy naming for standard borrowings not listed above.
DEBT_NAME = re.compile(
    r"(Debt|Borrowings?|NotesPayable|LoansPayable|CommercialPaper|LineOfCredit|SeniorNotes)"
)
NOT_DEBT_NAME = re.compile(
    r"(Securities|Receivable|Investment|InterestPayable|Fair|Derivative|Issuance|Proceeds|"
    r"Repayments|Payments|IncreaseDecrease|Amortization|Discount|Premium|Extinguishment|Gain|"
    r"Covenant|Description|Policy|Percentage|Rate|Term$|Instrument(?!CarryingAmount))"
)
# Labels for extension lines; deliberately conservative and issuer-neutral.
LABELS: tuple[tuple[Side, re.Pattern[str], Category, str | None], ...] = (
    (
        "liability",
        re.compile(
            r"(finance|capital) lease.*(debt|borrowing)|(debt|borrowing).*(finance|capital) lease"
        ),
        "borrowing_with_finance_lease",
        "includes finance lease liabilities",
    ),
    ("liability", re.compile(r"operating lease"), "operating_lease", None),
    ("liability", re.compile(r"(finance|capital) lease"), "finance_lease", None),
    (
        "liability",
        re.compile(
            r"\b(debt|borrowings?|notes? payable|commercial paper|loans? payable|credit facilit"
            r"|line of credit|revolv\w*|term loans?|convertible (senior )?notes|senior notes"
            r"|bonds? payable|debentures|promissory notes?)\b"
        ),
        "borrowing",
        None,
    ),
    (
        "liability",
        re.compile(r"accounts payable.*accrued|accounts payable and other"),
        "payables",
        "includes accrued liabilities",
    ),
    ("liability", re.compile(r"^(trade )?accounts payable\b"), "payables", None),
    (
        "equity",
        re.compile(r"non-?controlling interest|minority interest"),
        "minority_interest",
        None,
    ),
    ("equity", re.compile(r"preferred (stock|shares)"), "preferred_equity", None),
    ("asset", re.compile(r"^cash and cash equivalents$|^cash and equivalents$"), "cash", None),
    (
        "asset",
        re.compile(r"short-term investments|marketable securities"),
        "marketable_securities",
        None,
    ),
    (
        "asset",
        re.compile(r"^(trade )?(accounts )?receivables?\b|^accounts receivable"),
        "receivables",
        None,
    ),
    ("asset", re.compile(r"^inventor(y|ies)\b"), "inventory", None),
    (
        "asset",
        re.compile(r"property,? (plant|and equipment).*net|^property and equipment$"),
        "net_ppe",
        None,
    ),
    (
        "flow",
        re.compile(r"^(total )?(net )?(revenues?|sales|net sales|operating revenues?)$"),
        "revenue",
        None,
    ),
    ("flow", re.compile(r"^cost of (sales|revenues?|goods sold)"), "cogs", None),
    (
        "flow",
        re.compile(r"research|engineering and development|product development"),
        "rnd",
        None,
    ),
    (
        "flow",
        re.compile(
            r"^(operating (income|loss|profit)|income \(loss\) from operations|"
            r"(loss|income) from operations)"
        ),
        "operating_income",
        None,
    ),
    ("flow", re.compile(r"^interest expense"), "interest_expense", None),
    (
        "flow",
        re.compile(r"(income|earnings|loss)\b.*before (provision for )?income tax"),
        "pretax_income",
        None,
    ),
    (
        "flow",
        re.compile(
            r"(purchases? of|additions to|capital expenditures?|investments? in) "
            r"(property|premises|plant|fixed assets|equipment)|^capital expenditures?$"
        ),
        "capex",
        None,
    ),
    (
        "flow",
        re.compile(r"(stock|share|equity)-based compensation|stock compensation"),
        "stock_based_compensation",
        None,
    ),
    (
        "flow",
        re.compile(r"^depreciation|depreciation and amortization"),
        "depreciation_amortization",
        None,
    ),
    (
        "flow",
        re.compile(r"repurchases? of (common )?(stock|shares)|share repurchases?"),
        "buybacks",
        None,
    ),
    ("flow", re.compile(r"^(cash )?dividends paid|^payments? of dividends"), "dividends", None),
)
EQUITY_SIDE: set[Category] = {
    "equity",
    "equity_including_nci",
    "minority_interest",
    "temporary_equity",
    "preferred_equity",
    "other_equity",
}
LIABILITY_SIDE: set[Category] = {
    "payables",
    "borrowing",
    "borrowing_with_finance_lease",
    "operating_lease",
    "finance_lease",
    "total_liabilities",
    "other_liability",
}


def _ancestors(statement: Statement) -> dict[str, set[str]]:
    parents: dict[str, set[str]] = {}
    for arc in statement.calculations:
        parents.setdefault(arc.child, set()).add(arc.parent)
    result: dict[str, set[str]] = {}
    for concept in {arc.child for arc in statement.calculations}:
        seen: set[str] = set()
        stack = list(parents.get(concept, ()))
        while stack:
            parent = stack.pop()
            if parent not in seen:
                seen.add(parent)
                stack.extend(parents.get(parent, ()))
        result[concept] = seen
    return result


def balance_side(line: Line, package: XBRLPackage, ancestors: set[str]) -> Side:
    """Asset, liability or equity side from issuer sums, then balance type and label."""
    names = {a.split(":", 1)[1] for a in ancestors}
    if names & {"Assets", "AssetsCurrent", "AssetsNoncurrent"}:
        return "asset"
    if names & {"Liabilities", "LiabilitiesCurrent", "LiabilitiesNoncurrent"}:
        return "liability"
    if names & {
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",
    }:
        return "equity"
    label = line.label.lower()
    if re.search(r"equity|stock\b|shares|capital|retained|deficit|treasury|noncontrolling", label):
        return "equity"
    balance = package.elements.get(line.concept)
    if balance is not None and balance.balance == "debit":
        return "asset"
    return "liability"


def classify_statement_lines(
    package: XBRLPackage, kind: StatementKind, learned: dict[str, Category] | None = None
) -> tuple[Classified, ...]:
    """Every non-abstract line gets a category; extension lines may stay ``unknown``."""
    statement = package.statements[kind]
    ancestry = _ancestors(statement)
    # Lease location tags name the extension lines that carry lease liabilities.
    located: dict[str, Category] = {}
    lease_tags: tuple[tuple[str, Category], ...] = (
        ("us-gaap:OperatingLeaseLiability", "operating_lease"),
        ("us-gaap:FinanceLeaseLiability", "finance_lease"),
    )
    for (tag, _, _), named in package.enumerations.items():
        for prefix, lease_category in lease_tags:
            if tag.startswith(prefix) and tag.endswith(
                "StatementOfFinancialPositionExtensibleList"
            ):
                for element in named:
                    located.setdefault(element, lease_category)
    result = []
    for line in statement.lines:
        if line.abstract:
            continue
        name = line.concept.split(":", 1)[1]
        ancestors = ancestry.get(line.concept, set())
        side: Side = "flow" if kind != "balance_sheet" else balance_side(line, package, ancestors)
        known = STANDARD.get(name) if not line.custom else None
        if known is not None:
            category, basis = known
            if category == "temporary_equity":
                # Temporary equity is a claim; map it by what the issuer says it is.
                label = line.label.lower()
                if re.search(r"preferred|preference", label):
                    category, basis = "preferred_equity", "redeemable preferred in temporary equity"
                elif re.search(r"non-?controlling|minority", label):
                    category, basis = "minority_interest", "redeemable noncontrolling interest"
            if kind == "balance_sheet" and category in EQUITY_SIDE:
                side = "equity"
            elif kind == "balance_sheet" and category in LIABILITY_SIDE:
                side = "liability"
            result.append(Classified(line, category, side, "taxonomy", basis))
            continue
        if (
            not line.custom
            and kind == "balance_sheet"
            and side == "liability"
            and DEBT_NAME.search(name)
            and not NOT_DEBT_NAME.search(name)
        ):
            lease = re.search(r"(Capital|Finance)Lease", name)
            result.append(
                Classified(
                    line,
                    "borrowing_with_finance_lease" if lease else "borrowing",
                    side,
                    "pattern",
                    "includes finance lease liabilities" if lease else None,
                )
            )
            continue
        if line.custom and line.concept in located and "lease" in line.label.lower():
            result.append(
                Classified(line, located[line.concept], "liability", "label", "lease location tag")
            )
            continue
        matched = lease_match(package, line) if line.custom and side == "liability" else None
        if matched is not None:
            result.append(
                Classified(line, matched, "liability", "label", "matches lease disclosure")
            )
            continue
        if learned and line.concept in learned:
            result.append(Classified(line, learned[line.concept], side, "model"))
            continue
        if line.custom:
            label = line.label.lower().strip()
            match = next(
                (
                    (category, basis)
                    for rule_side, pattern, category, basis in LABELS
                    if rule_side == side and pattern.search(label)
                ),
                None,
            )
            if match is not None:
                result.append(Classified(line, match[0], side, "label", match[1]))
                continue
            result.append(Classified(line, "unknown", side, "default"))
            continue
        default: Category = {
            "asset": "other_asset",
            "liability": "other_liability",
            "equity": "other_equity",
            "flow": "other",
        }[side]  # type: ignore[assignment]
        result.append(Classified(line, default, side, "default"))
    if kind == "balance_sheet":
        result = mark_temporary_equity(result, ancestry)
    return tuple(result)


def lease_match(package: XBRLPackage, line: Line) -> Category | None:
    """A custom 'lease liability' line equal to the tagged lease disclosure for its date."""
    if "lease" not in line.label.lower():
        return None
    end = package.filing.report_date
    if end is None:
        return None
    amount = package.value(line.concept, None, end)
    if amount is None:
        return None
    for tag, category in (
        ("OperatingLeaseLiability", "operating_lease"),
        ("FinanceLeaseLiability", "finance_lease"),
    ):
        for suffix in ("", "Current", "Noncurrent"):
            disclosed = package.value(f"us-gaap:{tag}{suffix}", None, end)
            if disclosed is not None and abs(disclosed - amount) <= 1:
                return category  # type: ignore[return-value]
    return None


TEMPORARY_LABEL = re.compile(r"mezzanine|temporary equity|subject to (possible )?redemption")


def mark_temporary_equity(
    lines: list[Classified], ancestry: dict[str, set[str]]
) -> list[Classified]:
    """Claims inside a temporary-equity total keep that location in their basis."""
    totals = {
        c.line.concept
        for c in lines
        if c.category == "temporary_equity" or TEMPORARY_LABEL.search(c.line.label.lower())
    }
    marked = []
    for c in lines:
        inside = bool(ancestry.get(c.line.concept, set()) & totals)
        if inside and c.category in ("preferred_equity", "minority_interest") and not c.basis:
            c = Classified(c.line, c.category, c.side, c.source, "in temporary equity")
        marked.append(c)
    return marked
