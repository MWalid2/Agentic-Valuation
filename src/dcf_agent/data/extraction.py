"""Concept values and statuses for every period in one filing's XBRL package.

Each concept is ``reported`` (cited value, possibly a sum of cited lines),
``not_presented`` (positive evidence the statement is complete without it),
``not_disclosed`` (the filing does not provide it) or ``unresolved``. Lines are
summed only when the issuer's own calculations show they are additive.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from dcf_agent.contracts.inputs import FinancialConcept
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.concepts import (
    STANDARD,
    Category,
    Classified,
    _ancestors,
    classify_statement_lines,
)
from dcf_agent.data.sec import Fact
from dcf_agent.data.xbrl import NET_INCOME, StatementKind, XBRLPackage

Status = Literal["reported", "not_presented", "not_disclosed", "unresolved"]
Period = tuple[date | None, date]
# A captive finance arm: financial services/products members on a segment-type axis.
FINANCE_MEMBER = re.compile(
    r"financ\w*(services|products|segment|subsidiar|operations)|credit(corp|company|services)"
    r"|captivefinanc",
    re.I,
)
SEGMENT_AXIS = re.compile(r"Segment|ProductOrService|ConsolidatedEntities|ConsolidationItems", re.I)


# Share of a statement's lines that must carry values for the filing to present a period.
PRESENTED = 0.5
# Unclassified lines below this share of the scale (assets or revenue) cannot hide a concept.
MATERIALITY = 0.005
# An unclassified line can only hide a concept if its label could describe it.
HINTS: dict[FinancialConcept, str] = {
    "debt": r"debt|borrow|loan|notes?\b|credit|financ|bond|obligation|paper|revolv|bank",
    "goodwill": r"goodwill",
    "minority_interest": r"non-?controlling|minority|redeemable",
    "preferred_equity": r"preferred|preference|redeemable|convertible",
    "marketable_securities": r"investment|securities|marketable|deposit",
    "inventory": r"inventor|merchandise|materials|supplies|stock",
    "receivables": r"receivable",
    "payables": r"payable",
    "rnd": r"research|development|engineering|technology",
    "cogs": r"cost of|costs of",
    "sga": r"selling|general|administrative|marketing",
    "ebit": r"operating (income|loss|profit)|from operations",
    "pretax_income": r"before (provision for )?income tax",
    "income_tax_expense": r"income tax|provision for tax",
    "capex": r"property|equipment|capital expend|plant|construction|fixed assets",
    "dividends": r"dividend|distribution",
    "buybacks": r"repurchase|buy-?back|treasury|retire",
    "depreciation_amortization": r"depreciation|amortization|depletion",
    "stock_based_compensation": r"compensation|stock-based|share-based|equity award",
    "temporary_equity": r"redeemable|temporary|mezzanine|redemption",
}
# Cash-flow concepts live inside one section total of the issuer's calculation.
REGIONS: dict[FinancialConcept, set[str]] = {
    "dividends": {
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    },
    "buybacks": {
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
    },
    "capex": {
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    },
    "depreciation_amortization": {
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    },
    "stock_based_compensation": {
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    },
}
# Pretax components outside operations, used only when no operating-income line exists.
NONOPERATING = {
    "us-gaap:" + name
    for name in (
        "InterestExpense InterestExpenseNonoperating InterestExpenseDebt InterestAndDebtExpense "
        "InterestIncomeExpenseNonoperatingNet InterestIncomeExpenseNet "
        "OtherNonoperatingIncomeExpense "
        "NonoperatingIncomeExpense InvestmentIncomeInterest InvestmentIncomeNonoperating "
        "InterestAndOtherIncome OtherNonoperatingIncome OtherNonoperatingExpense "
        "GainsLossesOnExtinguishmentOfDebt IncomeLossFromEquityMethodInvestments "
        "NonoperatingGainsLosses InvestmentIncomeInterestAndDividend InterestIncomeOther "
        "ForeignCurrencyTransactionGainLossBeforeTax OtherComprehensiveIncomeLossNetOfTax"
    ).split()
}
NONOPERATING_LABEL = (
    r"interest|non-?operating|other (income|expense)|investment income|equity in|"
    r"extinguishment|foreign (currency|exchange)"
)
# Concepts whose standard elements are unambiguous when disclosed only in the notes.
NOTE_REPORTABLE: set[FinancialConcept] = {
    "goodwill",
    "minority_interest",
    "preferred_equity",
    "marketable_securities",
}
SIGNED: set[FinancialConcept] = {
    "ebit",
    "net_income",
    "pretax_income",
    "income_tax_expense",
    "equity",
    "minority_interest",
    "operating_cash_flow",
    "change_in_cash",
    "revenue",
}


@dataclass(frozen=True)
class ConceptValue:
    status: Status
    value: Sourced[float] | None = None
    basis: str | None = None
    elements: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class Extraction:
    package: XBRLPackage
    classifications: dict[StatementKind, tuple[Classified, ...]]
    periods: dict[Period, dict[FinancialConcept, ConceptValue]] = field(default_factory=dict)
    issues: dict[str, str] = field(default_factory=dict)

    def unknown_lines(self) -> tuple[Classified, ...]:
        return tuple(
            c for cs in self.classifications.values() for c in cs if c.category == "unknown"
        )


def family(category: Category) -> set[str]:
    return {f"us-gaap:{name}" for name, (c, _) in STANDARD.items() if c == category}


def extract(
    package: XBRLPackage, as_of: date, learned: dict[str, Category] | None = None
) -> Extraction:
    classifications = {
        kind: classify_statement_lines(package, kind, learned) for kind in package.statements
    }
    result = Extraction(package, classifications)
    facts = {(f"{f.namespace}:{f.tag}", f.start, f.end, f.unit): f for f in package.facts}
    labels = {c.line.concept: c.line.label for cs in classifications.values() for c in cs}

    def value(concept: str, start: date | None, end: date) -> tuple[float, str] | None:
        """Non-dimensional value, else the sum of members presented on the statement."""
        found = package.value(concept, start, end)
        if found is not None:
            return found, ""
        # Members of one axis shown on a statement (e.g. third-party and related-party
        # receivables) partition the line; multi-axis keys are note-level breakdowns.
        # Only members the statement itself shows: issuers may tag overlapping members
        # (e.g. a product member and a segment member for the same column).
        shown = set().union(*(statement.concepts for statement in package.statements.values()))
        single: dict[str, list[float]] = {}
        for key, amount in package.members(concept, start, end).items():
            if len(key) == 1 and key[0][1] in shown:
                single.setdefault(key[0][0], []).append(amount)
        presented = [axis for axis in single if axis in shown]
        if len(presented) == 1:
            axis = presented[0]
            return sum(single[axis]), f"sum of {axis} members"
        return None

    def sourced(
        parts: list[tuple[str, float, float, str]], period: Period, note: str
    ) -> Sourced[float]:
        """parts: (concept, sign, value, detail)."""
        start, end = period
        refs = []
        for concept, _, _, _ in parts:
            fact = facts.get((concept, start, end, "USD"))
            refs.append(
                fact.document_ref if fact and fact.document_ref else package.filing.accession
            )
        details = " | ".join(
            f"{'+' if sign > 0 else '-'} {labels.get(c, c)} ({c}) = {v:g}{'; ' + d if d else ''}"
            for c, sign, v, d in parts
        )
        single = len(parts) == 1 and parts[0][1] > 0 and not parts[0][3]
        return Sourced[float](
            value=sum(sign * v for _, sign, v, _ in parts),
            prov=Provenance(
                source="sec_xbrl" if single else "derived",
                ref=" | ".join(dict.fromkeys(r for r in refs if r)),
                as_of=as_of,
                period_end=end,
                filed=package.filing.filed,
                confidence="high" if single else "medium",
                note=f"{note}; {details}; accession={package.filing.accession}",
            ),
        )

    def complete(kind: StatementKind, period: Period) -> bool:
        """Every issuer sum for the period holds, counting children presented by member."""
        if kind not in package.statements:
            return False
        start, end = period
        statement = package.statements[kind]
        children: dict[str, list[tuple[str, float]]] = {}
        for arc in statement.calculations:
            children.setdefault(arc.parent, []).append((arc.child, arc.weight))
        checked = 0
        for parent, arcs in children.items():
            reported = value(parent, start if kind != "balance_sheet" else None, end)
            if reported is None:
                continue
            parts = [
                (weight, value(child, start if kind != "balance_sheet" else None, end))
                for child, weight in arcs
            ]
            present = [(w, v[0]) for w, v in parts if v is not None]
            if not present:
                continue
            # An unreported child is nil (often shown as a dash); the sum must still hold.
            precision = [
                package.decimals.get((c, start if kind != "balance_sheet" else None, end, "USD"))
                for c, _ in arcs
            ]
            coarsest = min((d for d in precision if d is not None), default=0)
            tolerance = 0.5 * 10 ** (-coarsest) * len(parts) + 1e-6
            if abs(reported[0] - sum(w * v for w, v in present)) > tolerance:
                return False
            checked += 1
        return checked > 0

    def scale(period: Period) -> float:
        start, end = period
        assets = package.value("us-gaap:Assets", None, end)
        revenue = next(
            (
                v
                for c in sorted(family("revenue"))
                if (v := package.value(c, start, end)) is not None
            ),
            None,
        )
        return abs((revenue if start is not None and revenue else assets) or 0.0)

    def material_unknown(
        kind: StatementKind, period: Period, side: str | None, concept: FinancialConcept
    ) -> list[str]:
        """Unclassified lines that are material and could plausibly contain the concept."""
        start, end = period
        threshold = MATERIALITY * scale(period)
        statement = package.statements.get(kind)
        subtotals = {a.parent for a in statement.calculations} if statement else set()
        ancestry = _ancestors(statement) if statement else {}
        hint = HINTS.get(concept)
        region = REGIONS.get(concept)
        flagged = []
        for c in classifications.get(kind, ()):
            if c.category != "unknown" or (side and c.side != side):
                continue
            if c.line.concept in subtotals:
                continue  # Its components are evaluated individually.
            if hint and not re.search(hint, c.line.label.lower()):
                continue
            names = {a.split(":", 1)[1] for a in ancestry.get(c.line.concept, set())}
            if region and names and not names & region:
                continue
            found = value(c.line.concept, None if kind == "balance_sheet" else start, end)
            if found is not None and abs(found[0]) >= threshold:
                flagged.append(f"{c.line.label} ({c.line.concept})")
        return flagged

    def note_facts(categories: set[Category], period: Period) -> list[Fact]:
        names = set().union(*(family(c) for c in categories))
        start, end = period
        return [
            f
            for f in package.facts
            if f"{f.namespace}:{f.tag}" in names and f.start == start and f.end == end and f.value
        ]

    def resolve(
        kind: StatementKind,
        categories: set[Category],
        period: Period,
        concept: FinancialConcept,
        *,
        additive: bool,
        signed: bool = False,
    ) -> ConceptValue | None:
        """None when no line of these categories carries a value for the period."""
        if kind not in package.statements:
            return ConceptValue("unresolved", reason=f"No {kind} identified")
        start, end = period
        ancestry = _ancestors(package.statements[kind])
        chosen: dict[str, tuple[Classified, float, str]] = {}
        for c in classifications[kind]:
            if c.category not in categories or c.line.concept in chosen:
                continue
            found = value(c.line.concept, start if kind != "balance_sheet" else None, end)
            if found is not None:
                chosen[c.line.concept] = (c, *found)
        # A broader subtotal (one with a basis note) yields to exact-basis lines inside it.
        for concept_name, (c, _, _) in list(chosen.items()):
            inside = [
                other
                for other, (oc, _, _) in chosen.items()
                if concept_name in ancestry.get(other, set()) and not oc.basis
            ]
            if c.basis and inside:
                del chosen[concept_name]
        # A line whose calculation ancestor is also selected is a component of it.
        lines = [
            item
            for concept_name, item in chosen.items()
            if not ancestry.get(concept_name, set()) & set(chosen)
        ]
        if not lines:
            return None
        if len(lines) > 1 and (
            not additive or any(not ancestry.get(c.line.concept) for c, _, _ in lines)
        ):
            if len({round(v) for _, v, _ in lines}) == 1:
                lines = lines[:1]  # The same amount presented twice, e.g. opening/closing rows.
            else:
                return ConceptValue(
                    "unresolved",
                    elements=tuple(c.line.concept for c, _, _ in lines),
                    reason="Several candidate lines without an issuer calculation proving they add",
                )
        parts = []
        for c, amount, detail in lines:
            if amount < 0 and concept not in SIGNED and not signed:
                if not c.line.custom:
                    return ConceptValue(
                        "unresolved",
                        elements=(c.line.concept,),
                        reason="Negative amount for a magnitude concept",
                    )
                amount, detail = -amount, (detail + "; " if detail else "") + "sign normalized"
            parts.append((c.line.concept, 1.0, amount, detail))
        bases = sorted({c.basis for c, _, _ in lines if c.basis})
        return ConceptValue(
            "reported",
            sourced(parts, period, f"{kind} {concept}"),
            basis="; ".join(bases) or None,
            elements=tuple(p[0] for p in parts),
        )

    def absent(
        kind: StatementKind,
        period: Period,
        side: str | None,
        categories: set[Category],
        what: str,
        concept: FinancialConcept,
    ) -> ConceptValue:
        """Not presented only with positive evidence that the statement is complete."""
        start, end = period
        notes = note_facts(categories, period)
        if notes and concept in NOTE_REPORTABLE and len({f.value for f in notes}) == 1:
            fact = notes[0]
            return ConceptValue(
                "reported",
                sourced(
                    [(f"{fact.namespace}:{fact.tag}", 1.0, fact.value, "")],
                    period,
                    f"note disclosure {concept}",
                ),
                basis="from notes; included in another statement line",
                elements=(f"{fact.namespace}:{fact.tag}",),
            )
        if notes:
            return ConceptValue(
                "unresolved",
                elements=tuple(f"{f.namespace}:{f.tag}" for f in notes),
                reason=f"{what} disclosed in notes but not presented as a statement line",
            )
        unknown = material_unknown(kind, period, side, concept)
        if unknown:
            return ConceptValue(
                "unresolved", reason="Unclassified material lines: " + "; ".join(unknown)
            )
        if not complete(kind, period):
            return ConceptValue(
                "unresolved", reason=f"{kind} calculations incomplete; absence not proven"
            )
        return ConceptValue(
            "not_presented", reason=f"No {what} line; statement calculations complete"
        )

    def from_notes(
        concept: FinancialConcept, tag: str, period: Period, location: str | None
    ) -> ConceptValue | None:
        """A disclosed total, or current plus noncurrent, from this filing's notes."""
        start, end = period
        total = package.value("us-gaap:" + tag, start, end)
        current = package.value(f"us-gaap:{tag}Current", start, end)
        noncurrent = package.value(f"us-gaap:{tag}Noncurrent", start, end)
        if total is None and (current is None or noncurrent is None):
            return None
        if total is not None:
            if (
                current is not None
                and noncurrent is not None
                and abs(total - current - noncurrent) > 1
            ):
                return ConceptValue("unresolved", reason="Note total disagrees with components")
            parts = [("us-gaap:" + tag, 1.0, total, "")]
        else:
            assert current is not None and noncurrent is not None
            parts = [
                (f"us-gaap:{tag}Current", 1.0, current, ""),
                (f"us-gaap:{tag}Noncurrent", 1.0, noncurrent, ""),
            ]
        if any(v < 0 for _, _, v, _ in parts):
            return ConceptValue("unresolved", reason="Negative disclosed amount")
        return ConceptValue(
            "reported",
            sourced(parts, period, f"note disclosure {concept}"),
            basis="from notes" + (f"; included in {location}" if location else ""),
            elements=tuple(p[0] for p in parts),
        )

    def location(tag: str, end: date) -> str | None:
        named: list[str] = []
        for suffix in ("", "Current", "Noncurrent"):
            key = (f"us-gaap:{tag}{suffix}StatementOfFinancialPositionExtensibleList", None, end)
            named.extend(package.enumerations.get(key, ()))
        return ", ".join(dict.fromkeys(labels.get(n, n) for n in named)) or None

    def derive_liabilities(period: Period) -> ConceptValue:
        """Liabilities from the issuer's own liabilities-and-equity calculation."""
        _, end = period
        statement = package.statements.get("balance_sheet")
        if statement is None:
            return ConceptValue("unresolved", reason="No balance sheet identified")
        children = [
            a
            for a in statement.calculations
            if a.parent == "us-gaap:LiabilitiesAndStockholdersEquity"
        ]
        sides = {c.line.concept: c for c in classifications["balance_sheet"]}
        if not children or not complete("balance_sheet", period):
            return ConceptValue(
                "unresolved",
                reason="No complete liabilities-and-equity calculation to derive liabilities",
            )
        parts = []
        for arc in children:
            c = sides.get(arc.child)
            found = value(arc.child, None, end)
            if c is None or found is None:
                continue
            # Only the side matters here; the issuer's calculation already places the line.
            if c.side == "liability":
                parts.append((arc.child, arc.weight, found[0], found[1]))
        if not parts:
            return ConceptValue("unresolved", reason="No liability lines in the calculation")
        return ConceptValue(
            "reported",
            sourced(
                parts,
                period,
                "total liabilities = liability-side lines of the issuer's calculation",
            ),
            elements=tuple(p[0] for p in parts),
        )

    def derived_ebit(period: Period, values: dict[FinancialConcept, ConceptValue]) -> ConceptValue:
        """Pretax income's own components, excluding nonoperating items.

        Used only when the statement has no operating-income line. Subtotals that
        contain nonoperating items (e.g. "total costs and other deductions" including
        interest) are opened using the issuer's calculation weights. Unclassified
        components inside pretax income are operating costs by construction and are
        named in the basis.
        """
        start, end = period
        current, pretax = values["ebit"], values["pretax_income"]
        statement = package.statements.get("income_statement")
        if statement is None or pretax.status != "reported" or len(pretax.elements) != 1:
            return current
        sides = {c.line.concept: c for c in classifications["income_statement"]}
        children: dict[str, list[tuple[str, float]]] = {}
        for arc in statement.calculations:
            children.setdefault(arc.parent, []).append((arc.child, arc.weight))

        def nonoperating(concept: str) -> bool:
            c = sides.get(concept)
            return concept in NONOPERATING or (
                c is not None
                and (
                    c.category in ("interest_expense", "interest_net")
                    or (c.line.custom and bool(re.search(NONOPERATING_LABEL, c.line.label.lower())))
                )
            )

        def contains_nonoperating(concept: str, seen: frozenset[str]) -> bool:
            return any(
                nonoperating(child)
                or (child not in seen and contains_nonoperating(child, seen | {child}))
                for child, _ in children.get(concept, [])
            )

        def leaves(concept: str, weight: float, seen: frozenset[str]) -> list[tuple[str, float]]:
            result = []
            for child, child_weight in children.get(concept, []):
                if (
                    child not in seen
                    and not nonoperating(child)
                    and contains_nonoperating(child, seen | {child})
                ):
                    result.extend(leaves(child, weight * child_weight, seen | {child}))
                else:
                    result.append((child, weight * child_weight))
            return result

        components = leaves(pretax.elements[0], 1.0, frozenset({pretax.elements[0]}))
        if len(components) < 2:
            return current
        parts, unclassified = [], []
        excluded = 0
        for concept, weight in components:
            found = value(concept, start, end)
            if found is None:
                continue
            if nonoperating(concept):
                excluded += 1
                continue
            c = sides.get(concept)
            if c is None:
                return current
            if c.category == "unknown":
                unclassified.append(c.line.label)
            parts.append((concept, weight, found[0], found[1]))
        if not parts or not excluded:
            return current
        basis = "derived: pretax income components excluding nonoperating items"
        if unclassified:
            basis += "; unclassified items treated as operating: " + ", ".join(unclassified)
        return ConceptValue(
            "reported",
            sourced(parts, period, "operating income"),
            basis=basis,
            elements=tuple(p[0] for p in parts),
        )

    def structural_pretax(
        period: Period, values: dict[FinancialConcept, ConceptValue]
    ) -> ConceptValue:
        """The amount income tax is subtracted from in the issuer's own calculation.

        Issuers sometimes tag this line with a broader element; its position in the
        calculation identifies it. Without any income-tax line, pretax equals net income.
        """
        start, end = period
        current = values["pretax_income"]
        statement = package.statements.get("income_statement")
        if statement is None:
            return current
        taxes = {
            c.line.concept
            for c in classifications["income_statement"]
            if c.category == "income_tax"
        }
        candidates = []
        for arc in statement.calculations:
            if arc.child not in taxes or arc.weight >= 0:
                continue
            siblings = [
                a
                for a in statement.calculations
                if a.parent == arc.parent
                and a.child != arc.child
                and a.weight > 0
                and "EquityMethod" not in a.child  # Equity-method income is after tax.
            ]
            if len(siblings) == 1:
                candidates.append(siblings[0].child)
        found = [(c, v) for c in dict.fromkeys(candidates) if (v := value(c, start, end))]
        if len(found) == 1:
            concept_name, (amount, detail) = found[0]
            return ConceptValue(
                "reported",
                sourced([(concept_name, 1.0, amount, detail)], period, "pretax income"),
                basis="identified as the amount before income tax in the issuer's calculation",
                elements=(concept_name,),
            )
        tax, net = values["income_tax_expense"], values["net_income"]
        if tax.status == "not_presented" and net.status == "reported" and net.value is not None:
            return ConceptValue(
                "reported",
                net.value,
                basis="no income tax presented; equals net income",
                elements=net.elements,
            )
        return current

    def interest(period: Period) -> ConceptValue:
        """Gross interest expense, then net interest, then cash interest paid (flagged bases)."""
        gross = resolve(
            "income_statement", {"interest_expense"}, period, "interest_expense", additive=True
        )
        if gross is not None and gross.status == "reported":
            return gross
        disclosed = note_facts({"interest_expense"}, period)
        if len({f.value for f in disclosed}) == 1:
            fact = disclosed[0]
            return ConceptValue(
                "reported",
                sourced(
                    [(f"{fact.namespace}:{fact.tag}", 1.0, fact.value, "")],
                    period,
                    "note disclosure interest",
                ),
                basis="from notes",
                elements=(f"{fact.namespace}:{fact.tag}",),
            )
        net = resolve(
            "income_statement",
            {"interest_net"},
            period,
            "interest_expense",
            additive=False,
            signed=True,
        )
        if net is None:
            net_facts = note_facts({"interest_net"}, period)
            if len({f.value for f in net_facts}) == 1:
                fact = net_facts[0]
                net = ConceptValue(
                    "reported",
                    sourced(
                        [(f"{fact.namespace}:{fact.tag}", 1.0, fact.value, "")],
                        period,
                        "net interest",
                    ),
                    elements=(f"{fact.namespace}:{fact.tag}",),
                )
        if net is not None and net.value is not None and net.value.value < 0:
            return ConceptValue(
                "reported",
                sourced([(net.elements[0], -1.0, net.value.value, "")], period, "interest expense"),
                basis="net of interest income",
                elements=net.elements,
            )
        start, end = period
        for tag in ("InterestPaidNet", "InterestPaid"):
            paid = package.value("us-gaap:" + tag, start, end)
            if paid is not None and paid >= 0:
                return ConceptValue(
                    "reported",
                    sourced([("us-gaap:" + tag, 1.0, paid, "")], period, "interest expense"),
                    basis="cash interest paid",
                    elements=("us-gaap:" + tag,),
                )
        return ConceptValue(
            "not_disclosed", reason="No gross, net-expense or cash interest amount for this period"
        )

    finance_members = {
        member
        for key in package.dimensional
        if len(key[4]) == 1
        and SEGMENT_AXIS.search(key[4][0][0].split(":", 1)[-1])
        and FINANCE_MEMBER.search((member := key[4][0][1]).split(":", 1)[-1])
    }

    def captive_split(period: Period, debt: ConceptValue) -> tuple[ConceptValue, ConceptValue]:
        """Industrial debt and captive-finance debt, when the statement splits them.

        A finance arm is recognised from segment or product members (e.g. financial
        services/products). Its debt is separable only if every selected borrowing line
        is presented by that axis on the balance sheet.
        """
        _, end = period
        if not finance_members:
            return debt, ConceptValue("not_presented", reason="No captive-finance segment")
        if debt.status != "reported" or debt.value is None:
            return debt, ConceptValue(
                "unresolved", reason="Captive finance arm present; consolidated debt unresolved"
            )
        statement = package.statements["balance_sheet"]
        axes = {c for c in statement.concepts if c.endswith("Axis")}
        best: tuple[str, list[tuple[str, float, float, str]]] | None = None
        for member in sorted(finance_members):
            parts: list[tuple[str, float, float, str]] = []
            split = True
            for element in debt.elements:
                members = {
                    k[0][1]: v
                    for k, v in package.members(element, None, end).items()
                    if len(k) == 1 and k[0][0] in axes and k[0][1] in statement.concepts
                }
                if not members:
                    split = False  # This borrowing line is not presented by segment.
                    break
                if member in members:
                    parts.append((element, 1.0, members[member], f"{member} member"))
            if (
                split
                and parts
                and (best is None or sum(p[2] for p in parts) > sum(p[2] for p in best[1]))
            ):
                best = (member, parts)
        if best is None:
            return debt, ConceptValue(
                "unresolved",
                reason="Captive finance arm present ("
                + ", ".join(sorted(m.split(":", 1)[-1] for m in finance_members))
                + "); its debt is not split on the balance sheet",
            )
        member, parts = best
        finance = sourced(parts, period, f"captive-finance debt ({member})")
        industrial = sourced(
            [(e, 1.0, v, "") for e, v in zip(debt.elements, line_values(debt, end), strict=True)]
            + [(e, -1.0, v, d) for e, _, v, d in parts],
            period,
            "industrial debt = consolidated debt less captive-finance debt",
        )
        basis = "; ".join(filter(None, (debt.basis, "excludes captive-finance debt")))
        return (
            ConceptValue("reported", industrial, basis=basis, elements=debt.elements),
            ConceptValue(
                "reported",
                finance,
                basis=f"debt of the {member.split(':', 1)[-1]} finance arm",
                elements=tuple(p[0] for p in parts),
            ),
        )

    def line_values(item: ConceptValue, end: date) -> list[float]:
        return [found[0] if (found := value(e, None, end)) else 0.0 for e in item.elements]

    def temporary_claims(
        period: Period, values: dict[FinancialConcept, ConceptValue]
    ) -> ConceptValue:
        """Temporary equity not already mapped to preferred stock or noncontrolling interest."""
        found = resolve(
            "balance_sheet", {"temporary_equity"}, period, "temporary_equity", additive=True
        )
        if found is None:
            return absent(
                "balance_sheet",
                period,
                "equity",
                {"temporary_equity"},
                "temporary equity",
                "temporary_equity",
            )
        if found.value is None:
            return found
        mapped = [
            values[c]
            for c in ("preferred_equity", "minority_interest")
            if values[c].value is not None and "temporary equity" in (values[c].basis or "")
        ]
        residual = found.value.value - sum(m.value.value for m in mapped if m.value is not None)
        if abs(residual) <= 1:
            return ConceptValue(
                "not_presented",
                reason="Temporary equity is entirely preferred stock or noncontrolling interest",
            )
        return ConceptValue(
            "reported",
            found.value
            if not mapped
            else Sourced[float](
                value=residual,
                prov=found.value.prov.model_copy(
                    update={
                        "source": "derived",
                        "note": (found.value.prov.note or "")
                        + "; less preferred/noncontrolling claims mapped separately",
                    }
                ),
            ),
            basis="redeemable shares or other claims in temporary equity",
            elements=found.elements,
        )

    # Balance-sheet dates with total assets, and flow periods per statement.
    def presents(kind: StatementKind, start: date | None, end: date) -> bool:
        """The statement itself shows this period: most of its lines carry a value.

        Notes repeat totals for earlier dates (e.g. segment assets); those dates are
        not statement periods of this filing.
        """
        lines = [c for c in classifications.get(kind, ()) if not c.line.abstract]
        if not lines:
            return False
        shown = sum(value(c.line.concept, start, end) is not None for c in lines)
        return shown >= PRESENTED * len(lines)

    instants = sorted(
        {
            f.end
            for f in package.facts
            if f.tag == "Assets"
            and f.namespace == "us-gaap"
            and f.start is None
            and presents("balance_sheet", None, f.end)
        }
    )
    for end in instants:
        period: Period = (None, end)
        values: dict[FinancialConcept, ConceptValue] = {}

        def single(
            concept: FinancialConcept,
            categories: set[Category],
            side: str,
            what: str,
            additive: bool = False,
            *,
            period: Period = period,
            values: dict[FinancialConcept, ConceptValue] = values,
        ) -> None:
            found = resolve("balance_sheet", categories, period, concept, additive=additive)
            values[concept] = found or absent(
                "balance_sheet", period, side, categories, what, concept
            )

        single("cash", {"cash"}, "asset", "cash")
        single("receivables", {"receivables"}, "asset", "receivables")
        single("inventory", {"inventory"}, "asset", "inventory", additive=True)
        single("net_ppe", {"net_ppe"}, "asset", "net PP&E")
        single("goodwill", {"goodwill"}, "asset", "goodwill")
        single("total_assets", {"total_assets"}, "asset", "total assets")
        single("payables", {"payables"}, "liability", "accounts payable")
        single("equity", {"equity"}, "equity", "parent equity")
        single(
            "minority_interest", {"minority_interest"}, "equity", "noncontrolling interest", True
        )
        single("preferred_equity", {"preferred_equity"}, "equity", "preferred stock", True)

        securities = resolve(
            "balance_sheet",
            {"marketable_securities"},
            period,
            "marketable_securities",
            additive=True,
        )
        cash_basis = values["cash"].basis or ""
        if securities is None:
            securities = from_notes(
                "marketable_securities", "MarketableSecurities", period, None
            ) or (
                ConceptValue("not_presented", reason="Included in the cash line")
                if "short-term investments" in cash_basis
                else absent(
                    "balance_sheet",
                    period,
                    "asset",
                    {"marketable_securities"},
                    "marketable securities",
                    "marketable_securities",
                )
            )
        values["marketable_securities"] = securities

        debt = resolve(
            "balance_sheet",
            {"borrowing", "borrowing_with_finance_lease"},
            period,
            "debt",
            additive=True,
        )
        unknown_liabilities = material_unknown("balance_sheet", period, "liability", "debt")
        if debt is not None and unknown_liabilities:
            debt = ConceptValue(
                "unresolved",
                elements=debt.elements,
                reason="Unclassified material liability lines may be borrowings: "
                + "; ".join(unknown_liabilities),
            )
        values["debt"] = debt or absent(
            "balance_sheet",
            period,
            "liability",
            {"borrowing", "borrowing_with_finance_lease"},
            "borrowing",
            "debt",
        )
        values["debt"], values["captive_finance_debt"] = captive_split(period, values["debt"])
        values["temporary_equity"] = temporary_claims(period, values)

        for concept, tag, category in (
            ("operating_lease_liabilities", "OperatingLeaseLiability", "operating_lease"),
            ("finance_lease_liabilities", "FinanceLeaseLiability", "finance_lease"),
        ):
            where = location(tag, end)
            lease = resolve("balance_sheet", {category}, period, concept, additive=True)  # type: ignore[arg-type]
            if lease is None:
                lease = from_notes(concept, tag, period, where)  # type: ignore[arg-type]
            if lease is None:
                lease = ConceptValue(
                    "not_disclosed",
                    reason="No lease liability presented or disclosed in this filing",
                )
            if concept == "finance_lease_liabilities" and "finance lease" in (
                values["debt"].basis or ""
            ):
                lease = ConceptValue(
                    lease.status,
                    lease.value,
                    "; ".join(filter(None, (lease.basis, "debt line includes finance leases"))),
                    lease.elements,
                    lease.reason,
                )
            values[concept] = lease  # type: ignore[index]

        liabilities = resolve(
            "balance_sheet", {"total_liabilities"}, period, "total_liabilities", additive=False
        )
        if liabilities is None:
            liabilities = derive_liabilities(period)
        values["total_liabilities"] = liabilities

        if values["equity"].status != "reported":
            including = resolve(
                "balance_sheet", {"equity_including_nci"}, period, "equity", additive=False
            )
            minority = values["minority_interest"]
            if (
                including is not None
                and including.value is not None
                and minority.status
                in (
                    "reported",
                    "not_presented",
                )
            ):
                parts = [(including.elements[0], 1.0, including.value.value, "")]
                if minority.value is not None:
                    parts.append(
                        (
                            minority.elements[0] if len(minority.elements) == 1 else "minority",
                            -1.0,
                            minority.value.value,
                            "",
                        )
                    )
                values["equity"] = ConceptValue(
                    "reported",
                    sourced(
                        parts, period, "parent equity = total equity less noncontrolling interest"
                    ),
                    elements=tuple(p[0] for p in parts),
                )
        result.periods[period] = values

    flows: dict[StatementKind, set[Period]] = {}
    flow_anchors: tuple[tuple[StatementKind, set[str]], ...] = (
        # Any bottom line or operating result anchors a period: a company without
        # minority holders may tag only ProfitLoss, a pre-revenue one has no revenue.
        (
            "income_statement",
            family("net_income") | family("revenue") | family("operating_income") | NET_INCOME,
        ),
        ("cash_flow", family("operating_cash_flow")),
    )
    for kind, anchors in flow_anchors:
        flows[kind] = {
            (f.start, f.end)
            for f in package.facts
            if f.start is not None
            and f"{f.namespace}:{f.tag}" in anchors
            and presents(kind, f.start, f.end)
        }

    def flow(
        kind: StatementKind,
        concept: FinancialConcept,
        categories: set[Category],
        period: Period,
        what: str,
        additive: bool = False,
        notes: bool = True,
    ) -> ConceptValue:
        found = resolve(kind, categories, period, concept, additive=additive)
        if found is not None:
            return found
        start, end = period
        disclosed = note_facts(categories, period) if notes else []
        if len({f.value for f in disclosed}) == 1:
            fact = disclosed[0]
            return ConceptValue(
                "reported",
                sourced(
                    [(f"{fact.namespace}:{fact.tag}", 1.0, fact.value, "")],
                    period,
                    f"note disclosure {concept}",
                ),
                basis="from notes; not a separate statement line",
                elements=(f"{fact.namespace}:{fact.tag}",),
            )
        if kind not in package.statements:
            return ConceptValue("unresolved", reason=f"No {kind} identified")
        unknown = material_unknown(kind, period, None, concept)
        if unknown:
            return ConceptValue(
                "unresolved", reason="Unclassified material lines: " + "; ".join(unknown)
            )
        if not complete(kind, period):
            return ConceptValue(
                "unresolved", reason=f"{kind} calculations incomplete; absence not proven"
            )
        return ConceptValue("not_presented", reason=f"No separate {what} line")

    for period in sorted(flows["income_statement"], key=lambda p: (p[1], p[0] or date.min)):
        values = result.periods.setdefault(period, {})
        values["revenue"] = flow(
            "income_statement", "revenue", {"revenue"}, period, "revenue", True
        )
        values["cogs"] = flow("income_statement", "cogs", {"cogs"}, period, "cost of revenue", True)
        values["sga"] = flow("income_statement", "sga", {"sga"}, period, "SG&A", True)
        values["rnd"] = flow("income_statement", "rnd", {"rnd"}, period, "R&D", True)
        values["ebit"] = flow(
            "income_statement",
            "ebit",
            {"operating_income"},
            period,
            "operating income",
            notes=False,
        )
        values["pretax_income"] = flow(
            "income_statement", "pretax_income", {"pretax_income"}, period, "pretax income"
        )
        values["income_tax_expense"] = flow(
            "income_statement", "income_tax_expense", {"income_tax"}, period, "income tax"
        )
        values["net_income"] = flow(
            "income_statement", "net_income", {"net_income"}, period, "net income"
        )
        if values["net_income"].status != "reported":
            common = resolve(
                "income_statement", {"net_income_common"}, period, "net_income", additive=False
            )
            if common is not None:
                values["net_income"] = common
        if values["net_income"].status != "reported":
            # Consolidated profit equals the parent's share only when the statement
            # attributes nothing to noncontrolling interests.
            statement = package.statements.get("income_statement")
            consolidated = package.value("us-gaap:ProfitLoss", *period)
            if (
                statement is not None
                and consolidated is not None
                and "us-gaap:ProfitLoss" in statement.concepts
                and not any(
                    "NoncontrollingInterest" in c or "MinorityInterest" in c
                    for c in statement.concepts
                )
            ):
                values["net_income"] = ConceptValue(
                    "reported",
                    sourced(
                        [("us-gaap:ProfitLoss", 1.0, consolidated, "")],
                        period,
                        "net income = consolidated profit; no noncontrolling interest presented",
                    ),
                    elements=("us-gaap:ProfitLoss",),
                )
        if values["pretax_income"].status != "reported":
            values["pretax_income"] = structural_pretax(period, values)
        if values["ebit"].status != "reported":
            values["ebit"] = derived_ebit(period, values)
        values["interest_expense"] = interest(period)
        if values["revenue"].status == "not_presented":
            values["revenue"] = ConceptValue(
                "not_presented",
                reason="No revenue line on a complete income statement (pre-revenue or shell)",
            )
    for period in sorted(flows["cash_flow"], key=lambda p: (p[1], p[0] or date.min)):
        values = result.periods.setdefault(period, {})
        cash_concepts: tuple[tuple[FinancialConcept, set[Category], str, bool], ...] = (
            ("operating_cash_flow", {"operating_cash_flow"}, "operating cash flow", False),
            ("depreciation_amortization", {"depreciation_amortization"}, "D&A", True),
            (
                "stock_based_compensation",
                {"stock_based_compensation"},
                "share-based compensation",
                True,
            ),
            ("capex", {"capex"}, "capital expenditure", True),
            ("buybacks", {"buybacks"}, "share repurchase", True),
            ("dividends", {"dividends"}, "dividend", True),
            ("change_in_cash", {"change_in_cash"}, "change in cash", False),
        )
        for concept, categories, what, additive in cash_concepts:
            values[concept] = flow("cash_flow", concept, categories, period, what, additive)
        if "interest_expense" in values and values["interest_expense"].status != "reported":
            values["interest_expense"] = interest(period)

    for period, values in result.periods.items():
        for concept, item in values.items():
            if item.status == "unresolved":
                result.issues[f"{period[0]}/{period[1]}:{concept}"] = item.reason
    return result
