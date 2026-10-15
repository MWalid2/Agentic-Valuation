"""Bounded, filing-wide statement recipes; no issuer names or reference amounts."""

from dataclasses import dataclass
from datetime import date

from dcf_agent.agents.snapshot import FieldChoice, validate_choice
from dcf_agent.contracts.inputs import FinancialConcept
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.sec import SECError
from dcf_agent.tools.edgar import EDGARTools, EvidenceCell, debt_candidate
from dcf_agent.tools.snapshot import Section


@dataclass(frozen=True)
class StatementValue:
    concept: FinancialConcept
    start: date | None
    end: date
    amount: Sourced[float]


def statement_values(
    tools: EDGARTools, statements: dict[Section, tuple[EvidenceCell, ...]]
) -> tuple[tuple[StatementValue, ...], dict[str, str]]:
    """Accepted recipe values plus a reason for every attempted recipe that failed."""
    result = []
    issues: dict[str, str] = {}

    def reject(concept: str, reason: str) -> None:
        issues[f"recipe:{tools.filing.accession}:{concept}"] = reason

    balance = {c.cell_id: c for c in statements["balance_sheet"]}
    income = statements["income_statement"]

    def select(choice: FieldChoice, ledger: dict[str, EvidenceCell], start: date) -> None:
        try:
            field = validate_choice(choice, ledger, tools, start)
        except SECError as error:
            reject(choice.concept, str(error))
            return
        if field.amount is not None and field.observed_on is not None:
            mapping: dict[str, FinancialConcept] = {
                "operating_income": "ebit",
                "debt": "debt",
                "total_liabilities": "total_liabilities",
            }
            concept = mapping[choice.concept]
            if concept == "total_liabilities":
                assets = [c.value for c in ledger.values() if c.tag == "us-gaap:Assets"]
                equity = [c.value for c in ledger.values() if c.tag == "us-gaap:StockholdersEquity"]
                other = sum(
                    c.value
                    for c in ledger.values()
                    if c.tag
                    in (
                        "us-gaap:MinorityInterest",
                        "us-gaap:TemporaryEquityCarryingAmountAttributableToParent",
                    )
                )
                if (
                    len(assets) != 1
                    or len(equity) != 1
                    or abs(assets[0] - equity[0] - other - field.amount.value) > 1
                ):
                    reject(choice.concept, "Liabilities do not satisfy the balance-sheet identity")
                    return
            result.append(
                StatementValue(concept, field.period_start, field.observed_on, field.amount)
            )

    if balance:
        end = next(iter(balance.values())).period_end
        debt = [i for i, c in balance.items() if debt_candidate(c)]
        if debt:
            select(FieldChoice(concept="debt", cell_ids=debt), balance, end)
        current = [i for i, c in balance.items() if c.tag == "us-gaap:LiabilitiesCurrent"]
        noncurrent = [i for i, c in balance.items() if c.tag == "us-gaap:LiabilitiesNoncurrent"]
        if len(current) == 1:
            if len(noncurrent) == 1:
                liabilities = current + noncurrent
            else:
                rows = list(balance)
                later = rows[rows.index(current[0]) + 1 :]
                liabilities = current + [
                    i
                    for i in later
                    if (
                        (
                            "Noncurrent" in balance[i].tag
                            and ("Liabilit" in balance[i].tag or "Debt" in balance[i].tag)
                        )
                        or balance[i].tag.split(":")[-1]
                        in (
                            "LongTermDebt",
                            "LongTermDebtAndCapitalLeaseObligations",
                            "DeferredIncomeTaxLiabilitiesNet",
                        )
                    )
                ]
            select(FieldChoice(concept="total_liabilities", cell_ids=liabilities), balance, end)
        # A finished-goods tag is a total only when the primary statement labels it
        # unambiguously as the whole inventory line, not a note's component.
        inventories = [
            c
            for c in balance.values()
            if c.label.lower().strip().rstrip(":")
            in ("inventories", "inventories, net", "inventory", "inventory, net")
            and c.tag in ("us-gaap:InventoryNet", "us-gaap:InventoryFinishedGoodsNetOfReserves")
        ]
        if len(inventories) == 1:
            cell = inventories[0]
            try:
                tools.reconcile((cell,))
            except SECError as error:
                reject("inventory", str(error))
            else:
                if cell.value >= 0:
                    result.append(
                        StatementValue(
                            "inventory",
                            None,
                            end,
                            Sourced[float](
                                value=cell.value,
                                prov=Provenance(
                                    source="sec_filing_text",
                                    ref=cell.ref,
                                    as_of=tools.as_of,
                                    period_end=end,
                                    filed=cell.filed,
                                    confidence="medium",
                                    note=f"Primary-statement total: {cell.label}; {cell.tag}",
                                ),
                            ),
                        )
                    )

    for start in sorted({c.period_start for c in income if c.period_start is not None}):
        ledger = {c.cell_id: c for c in income if c.period_start == start}
        direct = [i for i, c in ledger.items() if c.tag == "us-gaap:OperatingIncomeLoss"]
        if len(direct) == 1:
            select(FieldChoice(concept="operating_income", cell_ids=direct), ledger, start)
            continue
        gross = [i for i, c in ledger.items() if c.tag == "us-gaap:GrossProfit"]
        sga = [
            i
            for i, c in ledger.items()
            if c.tag == "us-gaap:SellingGeneralAndAdministrativeExpense"
        ]
        pretax = [
            i
            for i, c in ledger.items()
            if c.tag == "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
            "ExtraordinaryItemsNoncontrollingInterest"
        ]
        if len(gross) != 1 or len(sga) != 1 or len(pretax) != 1:
            continue
        rows = list(ledger)
        g, s, p = (rows.index(ids[0]) for ids in (gross, sga, pretax))
        if not g < s < p:
            continue
        components = [ledger[i] for i in rows[g + 1 : s]]
        if components and (
            any(c.value < 0 for c in components)
            or abs(sum(c.value for c in components) - ledger[sga[0]].value) > 1
        ):
            continue
        allowed_nonoperating = {
            "InterestIncomeExpenseNonoperatingNet",
            "OtherNonoperatingIncomeExpense",
            "InvestmentIncomeInterest",
            "InterestExpense",
            "InterestExpenseNonOperating",
        }
        if any(ledger[i].tag.split(":")[-1] not in allowed_nonoperating for i in rows[s + 1 : p]):
            continue
        select(
            FieldChoice(concept="operating_income", cell_ids=gross, subtract_ids=sga), ledger, start
        )
    return tuple(result), issues
