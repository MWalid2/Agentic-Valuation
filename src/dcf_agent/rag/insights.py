"""Filing insights the valuation uses, read in code from the filings (no model).

- Dilution: options outstanding (count and weighted-average exercise price, for the
  treasury-stock method) and unvested restricted or performance share units, from the
  latest filing that tags them at its own balance-sheet date; convertible notes
  (principal outstanding and conversion price, by instrument) for the if-converted test.
- Lease discount rate: the weighted-average operating lease discount rate.
- Marginal tax rate: the federal statutory rate plus the state and local line of the
  rate reconciliation (net of the federal benefit), averaged over the years shown.
- Captive-finance split: a supplemental consolidating balance sheet (industrial and
  financial-services columns), which filers often leave untagged; accepted only when
  every period column adds up across the groups and the consolidated borrowings equal
  the tagged consolidated debt.

Every value carries its filing and fact (or table) as evidence.
"""

import itertools
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean
from typing import Literal

from dcf_agent.contracts.accounting import (
    DilutiveSecurity,
    FilingCitation,
    FilingInsights,
    FilingItem,
    LeaseDisclosure,
)
from dcf_agent.contracts.inputs import ConceptStatus, FinancialPeriod, RawFinancials
from dcf_agent.contracts.provenance import Confidence, Provenance, Sourced
from dcf_agent.rag.filings import Cell, Chunk, FactRef
from dcf_agent.rag.index import FilingIndex

G = "us-gaap:"
OPTIONS = G + "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber"
STRIKE = (
    G + "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstanding"
    "WeightedAverageExercisePrice"
)
UNVESTED = (
    G + "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThan"
    "OptionsNonvestedNumber"
)
LEASE_RATE = G + "OperatingLeaseWeightedAverageDiscountRatePercent"
CONVERSION_PRICE = G + "DebtInstrumentConvertibleConversionPrice1"
DEBT_AXIS = G + "DebtInstrumentAxis"
# The principal outstanding of one instrument at the balance-sheet date, in order.
OUTSTANDING = tuple(
    G + c
    for c in (
        "DebtInstrumentCarryingAmount",
        "ConvertibleNotesPayable",
        "LongTermDebt",
        "DebtInstrumentFaceAmount",
    )
)
LEASE_LIABILITY = G + "OperatingLeaseLiability"
LEASE_PARTS = (G + "OperatingLeaseLiabilityCurrent", G + "OperatingLeaseLiabilityNoncurrent")
FEDERAL_RATE = G + "EffectiveIncomeTaxRateReconciliationAtFederalStatutoryIncomeTaxRate"
STATE_RATE = G + "EffectiveIncomeTaxRateReconciliationStateAndLocalIncomeTaxes"
FEDERAL_AMOUNT = G + "IncomeTaxReconciliationIncomeTaxExpenseBenefitAtFederalStatutoryIncomeTaxRate"
STATE_AMOUNT = G + "IncomeTaxReconciliationStateAndLocalIncomeTaxes"
STATE_EXPENSE = (
    G + "StateAndLocalIncomeTaxExpenseBenefitContinuingOperations",
    G + "CurrentStateAndLocalTaxExpenseBenefit",
)
AWARD_AXIS = G + "AwardTypeAxis"
MARGINAL_BOUNDS = (0.15, 0.35)  # outside this, the reconciliation is not a usable marginal rate
LEASE_RATE_BOUNDS = (0.0, 0.2)
# A captive finance arm's column in a consolidating statement.
FINANCE_GROUP = re.compile(r"financ|credit|capital", re.I)
CONSOLIDATED_GROUP = re.compile(r"consolidated", re.I)
ELIMINATION_GROUP = re.compile(r"eliminat|adjust|reclass", re.I)
DEBT_ROW = re.compile(r"borrowing|debt|notes payable|commercial paper", re.I)
NOT_DEBT_ROW = re.compile(
    r"interest|proceeds|payment|issu|repay|cost|expense|income|receivable|asset|total liab", re.I
)
DASH = re.compile(r"^[\-–—]+$")
BLANK = re.compile(r"[\s​ $%]+")
NUMBER = re.compile(r"^\(?-?\$?\s*(\d[\d,]*(?:\.\d+)?)\s*\)?$")


@dataclass(frozen=True)
class Filing:
    """One indexed filing and its parsed chunks."""

    accession: str
    form: str
    filed: date
    period_end: date
    url: str
    chunks: tuple[Chunk, ...]

    def facts(self, concept: str) -> list[tuple[Chunk, FactRef]]:
        return [
            (c, f)
            for c in self.chunks
            for f in c.facts
            if f.concept == concept and f.value is not None
        ]


def load(index: FilingIndex, accessions: Iterable[str]) -> list[Filing]:
    """The filings, latest balance-sheet date first."""
    filings = []
    for accession in accessions:
        row = index.db.execute(
            "SELECT form, filed, period_end, url FROM filings WHERE accession = ?", (accession,)
        ).fetchone()
        if row is None or not row[2] or row[2] == "None":
            continue
        ids = index.db.execute(
            "SELECT chunk_id FROM chunks WHERE accession = ? ORDER BY rowid", (accession,)
        ).fetchall()
        filings.append(
            Filing(
                accession=accession,
                form=row[0],
                filed=date.fromisoformat(row[1]),
                period_end=date.fromisoformat(row[2]),
                url=row[3],
                chunks=tuple(index.chunk(i) for (i,) in ids),
            )
        )
    return sorted(filings, key=lambda f: (f.period_end, f.filed), reverse=True)


def evidence(
    filing: Filing,
    chunk: Chunk,
    value: float,
    as_of: date,
    note: str,
    *,
    fact: FactRef | None = None,
    period_end: date | None = None,
    confidence: Confidence = "high",
    derived: bool = False,
) -> tuple[Sourced[float], FilingCitation]:
    anchor = fact.fact_id if fact is not None else chunk.anchor
    source: Literal["sec_xbrl", "sec_filing_text", "derived"] = (
        "derived" if derived else "sec_xbrl" if fact is not None else "sec_filing_text"
    )
    sourced = Sourced[float](
        value=value,
        prov=Provenance(
            source=source,
            ref=(f"{filing.url}#{anchor}" if anchor else filing.url)
            + f" (accession {filing.accession})",
            as_of=as_of,
            period_end=period_end or (fact.end if fact is not None else filing.period_end),
            filed=filing.filed,
            confidence=confidence,
            note=note,
        ),
    )
    citation = FilingCitation(
        accession=filing.accession,
        filed=filing.filed,
        section=chunk.section,
        table_id=chunk.chunk_id,
    )
    return sourced, citation


def instant(filing: Filing, concept: str) -> list[tuple[Chunk, FactRef]]:
    """Facts at the filing's own balance-sheet date."""
    return [
        (c, f) for c, f in filing.facts(concept) if f.end == filing.period_end and f.start is None
    ]


def total(found: Sequence[tuple[Chunk, FactRef]]) -> tuple[Chunk, FactRef] | None:
    """The single consolidated (non-dimensional) value, if the filing states one."""
    plain = [(c, f) for c, f in found if not f.members]
    if len({round(f.value or 0.0, 6) for _, f in plain}) != 1:
        return None
    return plain[0]


def by_member(found: Sequence[tuple[Chunk, FactRef]]) -> dict[str, tuple[Chunk, FactRef]]:
    """One-axis breakdown values by member, on the award-type axis when there are several."""
    single = [(c, f) for c, f in found if len(f.members) == 1]
    axes = {f.members[0][0] for _, f in single}
    if len(axes) > 1:
        if AWARD_AXIS not in axes:
            return {}
        single = [(c, f) for c, f in single if f.members[0][0] == AWARD_AXIS]
    out: dict[str, tuple[Chunk, FactRef]] = {}
    for c, f in single:
        member = f.members[0][1]
        if member in out and not close(out[member][1].value or 0.0, f.value or 0.0):
            return {}  # the same member with two values: ambiguous
        out.setdefault(member, (c, f))
    return out


def close(a: float, b: float, tolerance: float = 0.005) -> bool:
    return abs(a - b) <= tolerance * max(abs(a), abs(b), 1e-9)


def short(member: str) -> str:
    return member.split(":", 1)[-1].removesuffix("Member")


# Dilution ----------------------------------------------------------------------------


def options(filings: Sequence[Filing], as_of: date) -> tuple[list[DilutiveSecurity], list[str]]:
    """Options outstanding as treasury-stock tranches, from the latest filing that tags them."""
    for filing in filings:
        counts, strikes = instant(filing, OPTIONS), instant(filing, STRIKE)
        if not counts:
            continue
        count, strike = total(counts), total(strikes)
        if count is not None and strike is not None:
            pairs = [("all", count, strike)]
        else:
            by_count, by_strike = by_member(counts), by_member(strikes)
            pairs = [(m, by_count[m], by_strike[m]) for m in by_count if m in by_strike]
            if not pairs or len(pairs) != len(by_count):
                return [], [
                    f"options outstanding tagged in {filing.accession} without a usable "
                    "weighted-average exercise price: not included"
                ]
        securities = []
        for member, (chunk, n), (_, price) in pairs:
            count_value, _ = evidence(
                filing, chunk, n.value or 0.0, as_of, "options outstanding", fact=n
            )
            strike_value, citation = evidence(
                filing,
                chunk,
                price.value or 0.0,
                as_of,
                "weighted-average exercise price",
                fact=price,
            )
            securities.append(
                DilutiveSecurity(
                    security_id=f"options:{short(member)}" if member != "all" else "options",
                    kind="option",
                    count=count_value,
                    strike_price=strike_value,
                    citation=citation,
                )
            )
        return securities, []
    return [], ["no options outstanding tagged in the filings"]


def instrument(fact: FactRef) -> str | None:
    return next((m for axis, m in fact.members if axis == DEBT_AXIS), None)


def convertibles(
    filings: Sequence[Filing], as_of: date
) -> tuple[list[DilutiveSecurity], list[str]]:
    """Convertible notes outstanding: principal and conversion shares (principal / price)."""
    notes: list[str] = []
    for filing in filings:
        prices = filing.facts(CONVERSION_PRICE)
        if not prices:
            continue
        securities = []
        seen: set[str] = set()
        for chunk, price in prices:
            name = instrument(price)
            if name is None:
                notes.append(f"a conversion price in {filing.accession} names no instrument")
                continue
            if name in seen or not price.value:
                continue
            seen.add(name)
            principal = next(
                (
                    (c, f)
                    for concept in OUTSTANDING
                    for c, f in filing.facts(concept)
                    if instrument(f) == name and f.end == filing.period_end and f.value
                ),
                None,
            )
            if principal is None:
                notes.append(f"{short(name)}: nothing outstanding at {filing.period_end}")
                continue
            amount, citation = evidence(
                filing,
                principal[0],
                principal[1].value or 0.0,
                as_of,
                f"{short(name)} principal outstanding",
                fact=principal[1],
            )
            shares, _ = evidence(
                filing,
                chunk,
                (principal[1].value or 0.0) / price.value,
                as_of,
                f"{short(name)}: principal / conversion price {price.value:,.2f}",
                period_end=filing.period_end,
                derived=True,
            )
            securities.append(
                DilutiveSecurity(
                    security_id=f"convertible:{short(name)}",
                    kind="convertible",
                    count=shares,
                    citation=citation,
                    principal=amount,
                    conversion_shares=shares,
                    strike_price=evidence(
                        filing, chunk, price.value, as_of, "conversion price", fact=price
                    )[0],
                )
            )
        if securities or notes:
            return securities, notes
    return [], []


def unvested(filings: Sequence[Filing], as_of: date) -> tuple[list[DilutiveSecurity], list[str]]:
    """Unvested share units (RSUs, PSUs): the stated total, or the sum of the award types."""
    for filing in filings:
        found = instant(filing, UNVESTED)
        if not found:
            continue
        stated = total(found)
        notes: list[str] = []
        if stated is not None:
            parts = [("all", *stated)]
        else:
            members = by_member(found)
            if not members:
                return [], [f"unvested awards in {filing.accession} not on one axis: not included"]
            values = {m: f.value or 0.0 for m, (_, f) in members.items()}
            # A member equal to the sum of the others is their total.
            whole = next(
                (
                    m
                    for m, v in values.items()
                    if len(values) > 2 and close(v, sum(values.values()) - v)
                ),
                None,
            )
            chosen = [whole] if whole else list(members)
            parts = [(m, *members[m]) for m in chosen]
            if not whole and len(parts) > 1:
                notes.append("unvested awards: sum of " + ", ".join(short(m) for m in chosen))
        securities = []
        for member, chunk, fact in parts:
            value, citation = evidence(
                filing,
                chunk,
                fact.value or 0.0,
                as_of,
                f"unvested awards ({short(member)})" if member != "all" else "unvested awards",
                fact=fact,
                confidence="high" if member == "all" or len(parts) == 1 else "medium",
            )
            securities.append(
                DilutiveSecurity(
                    security_id=f"units:{short(member)}" if member != "all" else "units",
                    kind="psu" if re.search(r"perform", member, re.I) else "rsu",
                    count=value,
                    citation=citation,
                )
            )
        return securities, notes
    return [], ["no unvested share units tagged in the filings"]


# Leases and tax -----------------------------------------------------------------------


def operating_lease(filings: Sequence[Filing], as_of: date) -> LeaseDisclosure | None:
    """Operating lease liability and its weighted-average discount rate."""
    for filing in filings:
        rate = total(instant(filing, LEASE_RATE))
        if rate is None or not LEASE_RATE_BOUNDS[0] < (rate[1].value or 0) < LEASE_RATE_BOUNDS[1]:
            continue
        liability = total(instant(filing, LEASE_LIABILITY))
        if liability is not None:
            amount, citation = evidence(
                filing,
                liability[0],
                liability[1].value or 0.0,
                as_of,
                "operating lease liability",
                fact=liability[1],
            )
        else:
            parts = [total(instant(filing, c)) for c in LEASE_PARTS]
            if any(p is None for p in parts):
                continue
            current, noncurrent = (p for p in parts if p is not None)
            amount, citation = evidence(
                filing,
                current[0],
                (current[1].value or 0.0) + (noncurrent[1].value or 0.0),
                as_of,
                "operating lease liability: current plus noncurrent",
                period_end=filing.period_end,
                derived=True,
            )
        discount, _ = evidence(
            filing,
            rate[0],
            rate[1].value or 0.0,
            as_of,
            "weighted-average discount rate",
            fact=rate[1],
        )
        return LeaseDisclosure(
            lease_type="operating", liability=amount, discount_rate=discount, citation=citation
        )
    return None


def marginal_tax(filings: Sequence[Filing], as_of: date) -> tuple[FilingItem | None, str]:
    """Federal statutory rate plus the average state and local line, from the latest 10-K."""
    annual = next((f for f in filings if f.form.startswith("10-K")), None)
    if annual is None:
        return None, "no 10-K"

    def yearly(concept: str) -> dict[date, tuple[Chunk, FactRef]]:
        out: dict[date, tuple[Chunk, FactRef]] = {}
        for c, f in annual.facts(concept):
            if f.members or f.start is None or f.end is None:
                continue
            if timedelta(days=340) < f.end - f.start < timedelta(days=390):
                out.setdefault(f.end, (c, f))
        return out

    federal_rates, state_rates = yearly(FEDERAL_RATE), yearly(STATE_RATE)
    federal_amounts, state_amounts = yearly(FEDERAL_AMOUNT), yearly(STATE_AMOUNT)
    expenses = {concept: yearly(concept) for concept in STATE_EXPENSE}
    years = sorted(set(federal_rates) | set(federal_amounts), reverse=True)
    if not years:
        return None, "no federal statutory line in the tax rate reconciliation"
    latest = years[0]
    federal = federal_rates[latest][1].value if latest in federal_rates else None
    federal = federal if federal is not None and 0.1 < federal < 0.4 else 0.21
    state: dict[date, float] = {}
    from_expense = False
    losses = 0
    for year in years:
        at_statutory = federal_amounts[year][1].value if year in federal_amounts else None
        if at_statutory is not None and at_statutory <= 0:
            losses += 1  # a pretax loss turns the reconciliation's signs: not a marginal rate
            continue
        rate = federal_rates[year][1].value if year in federal_rates else federal
        # Pretax income is the federal amount at the statutory rate over that rate.
        pretax = at_statutory / rate if at_statutory and rate else None
        if year in state_rates:
            state[year] = state_rates[year][1].value or 0.0
        elif year in state_amounts and pretax:
            state[year] = (state_amounts[year][1].value or 0.0) / pretax
        elif pretax:
            # No reconciliation line: state tax expense, net of its federal deduction.
            expense = next((e[year][1].value for e in expenses.values() if year in e), None)
            if expense is not None:
                state[year] = expense * (1 - (rate or federal)) / pretax
                from_expense = True
    if not state:
        if losses == len(years):
            return None, "pretax losses in every year of the tax rate reconciliation"
        return None, "no state and local line in the tax rate reconciliation"
    average_state = max(mean(state.values()), 0.0)
    marginal = federal + average_state
    if not MARGINAL_BOUNDS[0] <= marginal <= MARGINAL_BOUNDS[1]:
        return None, f"reconciliation gives {marginal:.1%}, outside {MARGINAL_BOUNDS}"
    chunk = (federal_rates.get(latest) or federal_amounts[latest])[0]
    shown = ", ".join(f"FY{y.year} {v:.2%}" for y, v in sorted(state.items()))
    description = (
        f"federal statutory {federal:.1%} plus state and local {average_state:.2%} "
        f"(net of the federal benefit; average of {shown}"
        + ("; partly from state tax expense" if from_expense else "")
        + ")"
    )
    value, citation = evidence(
        annual,
        chunk,
        marginal,
        as_of,
        description,
        period_end=latest,
        confidence="medium",
        derived=True,
    )
    return FilingItem(
        item_id="marginal_tax_rate",
        description=description,
        citation=citation,
        confidence="medium",
        amount=value,
    ), description


# Captive-finance split --------------------------------------------------------------------


@dataclass(frozen=True)
class CaptiveSplit:
    finance: float  # in dollars
    consolidated: float
    rows: tuple[str, ...]
    chunk: Chunk
    filing: Filing
    group: str


def tokens(row: Sequence[Cell]) -> list[str]:
    return [c.text.strip() for c in row if BLANK.sub("", c.text)]


def amount(text: str) -> float | None:
    text = text.strip()
    if DASH.match(text):
        return 0.0
    match = NUMBER.match(text)
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    return -value if text.startswith("(") or text.startswith("-") else value


def groups(chunk: Chunk) -> tuple[list[str], int] | None:
    """Group names (left to right) and the header row that holds them."""
    header = [tokens(r) for r in chunk.rows[: chunk.header_rows]]
    for i, row in enumerate(header):
        if any(CONSOLIDATED_GROUP.search(t) for t in row) and len(row) >= 3:
            above = header[i - 1] if i > 0 else []
            if above and len(above) <= len(row) and not any(re.search(r"\d", t) for t in above):
                names = [(above[j] + " " if j < len(above) else "") + t for j, t in enumerate(row)]
            else:
                names = row
            return names, i
    return None


def current_column(
    chunk: Chunk, after: int, count: int, period_end: date
) -> tuple[int, int] | None:
    """(periods per group, the period end's position) from the year and date header rows."""
    header = [tokens(r) for r in chunk.rows[: chunk.header_rows]]
    for i in range(after + 1, len(header)):
        years = header[i]
        if not years or not all(re.fullmatch(r"(19|20)\d\d", t) for t in years):
            continue
        if len(years) % count:
            return None
        per = len(years) // count
        first = years[:per]
        hits = [j for j, y in enumerate(first) if int(y) == period_end.year]
        if len(hits) > 1 and i > 0:
            dates = header[i - 1]
            day = f"{period_end:%b} {period_end.day}"
            hits = [
                j
                for j in hits
                if j < len(dates)
                and re.match(rf"{period_end:%b}\w*\.? {period_end.day}\b", dates[j])
            ] or [j for j in hits if j < len(dates) and dates[j] == day]
        return (per, hits[0]) if len(hits) == 1 else None
    return None


def guess_groups(row: Sequence[Cell], count: int) -> list[float]:
    """Each number's group, estimated from its position across the row (columns align)."""
    width = max(len(row) - 1, 1)
    return [
        min((i - 1) / width * count, count - 1)
        for i, cell in enumerate(row)
        if i > 0 and BLANK.sub("", cell.text) and amount(cell.text.strip()) is not None
    ]


def placement_cost(
    solution: dict[int, float], per: int, eliminations: set[int], where: list[float]
) -> tuple[int, float]:
    """(numbers placed as eliminations, distance from their positions in the row)."""
    slots = sorted(solution)
    moved = sum(abs(slot // per - group) for slot, group in zip(slots, where, strict=False))
    return sum(slot // per in eliminations for slot in slots), moved


def assign(numbers: list[float], count: int, per: int, consolidated: int) -> list[dict[int, float]]:
    """Every placement of a row's numbers in (group, period) slots where all columns add up.

    Blank cells are omitted in filings, so a row may hold fewer numbers than slots; each
    group's numbers keep their left-to-right order.
    """
    solutions: list[dict[int, float]] = []
    slots = [list(itertools.combinations(range(per), k)) for k in range(per + 1)]

    def place(group: int, start: int, table: list[dict[int, float]]) -> None:
        if group == count:
            if start != len(numbers):
                return
            for p in range(per):
                parts = sum(table[g].get(p, 0.0) for g in range(count) if g != consolidated)
                if abs(parts - table[consolidated].get(p, 0.0)) > 0.5 * count:
                    return
            if all(p in table[consolidated] for p in range(per) if any(p in t for t in table)):
                solutions.append(
                    {g * per + p: v for g, t in enumerate(table) for p, v in t.items()}
                )
            return
        left = len(numbers) - start
        for k in range(min(per, left) + 1):
            if left - k > per * (count - group - 1):
                continue
            for chosen in slots[k]:
                place(
                    group + 1,
                    start + k,
                    [*table, dict(zip(chosen, numbers[start : start + k], strict=True))],
                )

    place(0, 0, [])
    return solutions


def read_captive(chunk: Chunk, filing: Filing, consolidated_debt: float) -> CaptiveSplit | str:
    found = groups(chunk)
    if found is None:
        return "no group header"
    names, at = found
    finance = [i for i, n in enumerate(names) if FINANCE_GROUP.search(n)]
    whole = [i for i, n in enumerate(names) if CONSOLIDATED_GROUP.search(n)]
    if len(finance) != 1 or len(whole) != 1:
        return f"groups not recognised: {names}"
    column = current_column(chunk, at, len(names), filing.period_end)
    if column is None:
        return "no column for the balance-sheet date"
    per, current = column
    f_slot, c_slot = finance[0] * per + current, whole[0] * per + current
    finance_total = consolidated_total = 0.0
    rows: list[str] = []
    eliminations = {i for i, n in enumerate(names) if ELIMINATION_GROUP.search(n)}
    for row in chunk.rows[chunk.header_rows :]:
        cells = tokens(row)
        if not cells or not DEBT_ROW.search(cells[0]) or NOT_DEBT_ROW.search(cells[0]):
            continue
        numbers = [amount(t) for t in cells[1:]]
        if not numbers or any(n is None for n in numbers):
            return f"unreadable row: {cells[0]}"
        solutions = assign([n for n in numbers if n is not None], len(names), per, whole[0])
        if not solutions:
            return f"row does not add up: {cells[0]}"
        # Arithmetic cannot tell a blank group from a blank elimination, or which of two
        # groups holds a lone amount: prefer the fewest eliminations, then the placement
        # closest to where each number sits in the row.
        where = guess_groups(row, len(names))
        keys = [placement_cost(x, per, eliminations, where) for x in solutions]
        picks = {
            (x.get(f_slot, 0.0), x.get(c_slot))
            for x, k in zip(solutions, keys, strict=True)
            if k == min(keys)
        }
        if len(picks) != 1:
            return f"row does not add up uniquely: {cells[0]}"
        part, whole_value = picks.pop()
        if whole_value is None:
            return f"no consolidated value: {cells[0]}"
        finance_total += part
        consolidated_total += whole_value
        rows.append(cells[0])
    if not rows:
        return "no borrowing rows"
    scale = next(
        (s for s in (1.0, 1e3, 1e6, 1e9) if close(consolidated_total * s, consolidated_debt)), None
    )
    if scale is None:
        return (
            f"consolidated borrowings in the table ({consolidated_total:,.0f}) do not match the "
            f"consolidated debt ({consolidated_debt:,.0f})"
        )
    return CaptiveSplit(
        finance=finance_total * scale,
        consolidated=consolidated_total * scale,
        rows=tuple(rows),
        chunk=chunk,
        filing=filing,
        group=names[finance[0]],
    )


def captive_split(filing: Filing, consolidated_debt: float) -> CaptiveSplit | str:
    """The finance arm's borrowings from a consolidating balance sheet in the filing."""
    reasons = []
    for chunk in filing.chunks:
        if chunk.kind != "table" or not chunk.header_rows:
            continue
        head = " ".join(" ".join(tokens(r)) for r in chunk.rows[: chunk.header_rows])
        if not (FINANCE_GROUP.search(head) and CONSOLIDATED_GROUP.search(head)):
            continue
        if not any(tokens(r) and DEBT_ROW.search(tokens(r)[0]) for r in chunk.rows):
            continue
        found = read_captive(chunk, filing, consolidated_debt)
        if isinstance(found, CaptiveSplit):
            return found
        reasons.append(found)
    return "; ".join(reasons) or "no consolidating balance sheet"


def apply_captive(
    raw: RawFinancials, filings: Sequence[Filing], as_of: date
) -> tuple[RawFinancials, list[FilingItem], list[str]]:
    """Resolve unresolved captive-finance debt from the filings' consolidating statements."""
    items: list[FilingItem] = []
    notes: list[str] = []
    by_end = {f.period_end: f for f in filings}

    def fix(period: FinancialPeriod) -> FinancialPeriod:
        status = period.statuses.get("captive_finance_debt")
        debt = period.values.get("debt")
        if status is None or status.status != "unresolved" or debt is None:
            return period
        filing = by_end.get(period.period_end)
        if filing is None:
            return period
        found = captive_split(filing, debt.value)
        if isinstance(found, str):
            notes.append(f"{period.kind} {period.period_end}: captive split not read ({found})")
            return period
        description = (
            f"borrowings of the {found.group.lower()} column ({', '.join(found.rows)}) in the "
            "supplemental consolidating balance sheet; every column adds up and consolidated "
            "borrowings equal the tagged debt"
        )
        finance, citation = evidence(
            found.filing,
            found.chunk,
            found.finance,
            as_of,
            description,
            period_end=period.period_end,
            confidence="medium",
        )
        industrial = Sourced[float](
            value=debt.value - found.finance,
            prov=Provenance(
                source="derived",
                ref=finance.prov.ref,
                as_of=as_of,
                period_end=period.period_end,
                confidence="medium",
                note="industrial debt = consolidated debt less captive-finance debt",
            ),
        )
        items.append(
            FilingItem(
                item_id=f"captive_finance_debt:{period.kind}:{period.period_end}",
                description=description,
                citation=citation,
                confidence="medium",
                amount=finance,
            )
        )
        basis = "; ".join(
            filter(
                None,
                (
                    period.statuses.get("debt", ConceptStatus(status="reported")).basis,
                    "excludes captive-finance debt",
                ),
            )
        )
        return FinancialPeriod.model_validate(
            {
                **dict(period),
                "values": {**period.values, "debt": industrial, "captive_finance_debt": finance},
                "statuses": {
                    **period.statuses,
                    "debt": ConceptStatus(status="reported", basis=basis),
                    "captive_finance_debt": ConceptStatus(
                        status="reported",
                        basis=f"debt of the {found.group.lower()} arm (filing text)",
                    ),
                },
            }
        )

    fixed = raw.model_copy(
        update={
            "annual": tuple(fix(p) for p in raw.annual),
            "quarterly": tuple(fix(p) for p in raw.quarterly),
            "ttm": fix(raw.ttm) if raw.ttm is not None else None,
        }
    )
    return fixed, items, notes


# Everything --------------------------------------------------------------------------------


def read_insights(
    filings: Sequence[Filing], cik: str, as_of: date, raw: RawFinancials | None
) -> tuple[FilingInsights, RawFinancials | None, list[str]]:
    """Insights for the valuation, and the raw financials with any captive split applied."""
    notes: list[str] = []
    securities: list[DilutiveSecurity] = []
    for reader in (options, unvested, convertibles):
        found, why = reader(filings, as_of)
        securities += found
        notes += why
    lease = operating_lease(filings, as_of)
    if lease is None:
        notes.append("no operating lease discount rate tagged")
    tax, tax_note = marginal_tax(filings, as_of)
    if tax is None:
        notes.append(f"marginal tax not read: {tax_note}")
    captive: list[FilingItem] = []
    if raw is not None:
        raw, captive, why_not = apply_captive(raw, filings, as_of)
        notes += why_not
    insights = FilingInsights(
        cik=cik,
        as_of=as_of,
        dilutive_securities=tuple(securities),
        lease_liabilities=(lease,) if lease else (),
        tax_items=(tax,) if tax else (),
        captive_finance=tuple(captive),
    )
    return insights, raw, notes
