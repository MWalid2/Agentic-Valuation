"""Dated SEC history, fiscal-quarter differencing, and trailing-twelve-month flows."""

import re
from datetime import date, timedelta
from typing import get_args

from dcf_agent.contracts.inputs import (
    ConceptStatus,
    FinancialConcept,
    FinancialPeriod,
    RawFinancials,
)
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.extraction import ConceptValue, Extraction
from dcf_agent.data.financials import RULES, TOTAL_COMPONENTS
from dcf_agent.data.sec import Fact, Filing, SECError
from dcf_agent.data.statement_values import StatementValue

DAY = timedelta(days=1)
STOCKS: set[FinancialConcept] = {
    "cash",
    "marketable_securities",
    "receivables",
    "inventory",
    "payables",
    "net_ppe",
    "goodwill",
    "debt",
    "operating_lease_liabilities",
    "finance_lease_liabilities",
    "equity",
    "minority_interest",
    "preferred_equity",
    "temporary_equity",
    "captive_finance_debt",
    "total_assets",
    "total_liabilities",
    "shares_outstanding",
}
SIGNED: set[FinancialConcept] = {
    "ebit",
    "net_income",
    "pretax_income",
    "income_tax_expense",
    "equity",
    "operating_cash_flow",
    "change_in_cash",
}
TAGS: dict[FinancialConcept, tuple[str, ...]] = {c: tags for c, (_, tags) in RULES.items()}
TAGS.update(
    {
        "revenue": (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ),
        "ebit": ("OperatingIncomeLoss",),
        "net_income": ("NetIncomeLoss",),
        "cash": ("CashAndCashEquivalentsAtCarryingValue",),
        "total_assets": ("Assets",),
        "total_liabilities": ("Liabilities",),
        "equity": ("StockholdersEquity",),
        "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
        "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
        "shares_outstanding": ("CommonStockSharesOutstanding",),
        "change_in_cash": (
            "CashAndCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        ),
    }
)
# Statement-line bases broader or narrower than the concept; never override an exact value.
BROADER = re.compile(
    r"includes restricted cash|includes accrued|includes short-term investments|"
    r"includes other receivables|net of interest income|cash interest paid|"
    r"right-of-use|continuing operations only|includes accretion"
)
MEASURES = (
    "includes",
    "excludes",
    "net of",
    "cash interest paid",
    "continuing operations only",
)


def measurement_basis(basis: str | None) -> str | None:
    """The part of a basis note that changes what is measured, not where it came from.

    "from notes; included in other liabilities" is provenance; "includes accrued
    liabilities" changes the amount and must be consistent across periods.
    """
    parts = [p.strip() for p in (basis or "").split(";")]
    measured = [p for p in parts if p.startswith(MEASURES)]
    return "; ".join(measured) or None


# Balances a quarterly filing often omits; a recent annual observation may stand in.
CARRY_WINDOW = timedelta(days=371)
VALUED = ("reported", "carried_forward")


def settle(
    values: dict[FinancialConcept, Sourced[float]], statuses: dict[FinancialConcept, ConceptStatus]
) -> dict[FinancialConcept, ConceptStatus]:
    """Statuses consistent with values: every value has a valued status and vice versa."""
    result = {c: s for c, s in statuses.items() if (s.status in VALUED) == (c in values)}
    for concept in values:
        result.setdefault(concept, ConceptStatus(status="reported"))
    return result


CALENDAR_TAGS = set(TAGS["revenue"] + TAGS["net_income"] + TAGS["operating_cash_flow"])


def derived(
    parts: list[tuple[float, Sourced[float]]], end: date, as_of: date, note: str
) -> Sourced[float]:
    """Retain every dependency, including the original duration and filing vintage."""
    filed = [v.prov.filed for _, v in parts if v.prov.filed is not None]
    return Sourced[float](
        value=sum(sign * v.value for sign, v in parts),
        prov=Provenance(
            source="derived",
            as_of=as_of,
            period_end=end,
            filed=max(filed) if filed else None,
            confidence="medium",
            ref=" | ".join(v.prov.ref for _, v in parts),
            note=note
            + "; "
            + " | ".join(
                f"{sign:+g} * {v.value:g}; filed={v.prov.filed}; {v.prov.note}" for sign, v in parts
            ),
        ),
    )


_NONE = ConceptStatus(status="unresolved")


def build_ttm(quarters: tuple[FinancialPeriod, ...], as_of: date) -> FinancialPeriod | None:
    """Only four contiguous fiscal quarters; stocks come from the latest quarter."""
    selected = sorted(quarters, key=lambda p: p.period_end)[-4:]
    if len(selected) != 4 or any(p.kind != "quarterly" for p in selected):
        return None
    if not 350 <= (selected[-1].period_end - selected[0].period_start).days + 1 <= 380:
        return None
    for previous, current in zip(selected[:-1], selected[1:], strict=True):
        if (
            previous.period_end + DAY != current.period_start
            or current.fiscal_quarter != (previous.fiscal_quarter or 0) % 4 + 1
            or current.fiscal_year != previous.fiscal_year + (previous.fiscal_quarter == 4)
        ):
            return None
    latest = selected[-1]
    values = {c: v for c, v in latest.values.items() if c in STOCKS}
    statuses = {c: s for c, s in latest.statuses.items() if c in STOCKS}
    common = set.intersection(*(set(p.values) for p in selected)) - STOCKS
    if "revenue" not in common:
        return None
    for concept in common:
        values[concept] = derived(
            [(1.0, p.values[concept]) for p in selected],
            latest.period_end,
            as_of,
            f"TTM sum of four consecutive quarters: "
            f"{selected[0].period_start} to {latest.period_end}",
        )
        bases = sorted({b for p in selected if (b := p.statuses.get(concept, _NONE).basis)})
        statuses[concept] = ConceptStatus(status="reported", basis="; ".join(bases) or None)
    for concept in {c for p in selected for c in p.statuses} - STOCKS - common:
        kinds = {p.statuses[concept].status if concept in p.statuses else None for p in selected}
        if kinds <= {"reported", "not_presented"} and "reported" in kinds:
            # Quarters without the line are evidenced zeros; the window still sums.
            values[concept] = derived(
                [(1.0, p.values[concept]) for p in selected if concept in p.values],
                latest.period_end,
                as_of,
                f"TTM sum of four consecutive quarters: {selected[0].period_start} to "
                f"{latest.period_end}; quarters without the line are not presented (zero)",
            )
            statuses[concept] = ConceptStatus(
                status="reported", basis="derived with evidenced zero quarters"
            )
        elif kinds == {"not_presented"}:
            statuses[concept] = ConceptStatus(
                status="not_presented", reason="Not presented in any of the four quarters"
            )
        elif "unresolved" in kinds or None in kinds:
            statuses[concept] = ConceptStatus(
                status="unresolved", reason="Unresolved in at least one quarter of the window"
            )
        else:
            statuses[concept] = ConceptStatus(
                status="not_disclosed", reason="Not available for every quarter in the window"
            )
    return FinancialPeriod(
        kind="ttm",
        fiscal_year=latest.fiscal_year,
        period_start=selected[0].period_start,
        period_end=latest.period_end,
        values=values,
        statuses=settle(values, statuses),
    )


def harmonize(
    annual: list[FinancialPeriod],
    quarters: list[FinancialPeriod],
    alternatives: dict[tuple[date, FinancialConcept], tuple[Sourced[float], str]],
    issues: dict[str, str],
) -> tuple[list[FinancialPeriod], list[FinancialPeriod]]:
    """One basis per balance across all periods.

    When some period only offers a broader statement line (e.g. payables including
    accrued liabilities) and every other period has that same line available, the
    whole series switches to it. Otherwise the mixed basis is reported, not hidden.
    """
    periods = [*annual, *quarters]
    updates: dict[int, dict[FinancialConcept, tuple[Sourced[float], str]]] = {}
    for concept in {c for _, c in alternatives}:
        bases = {
            measurement_basis(p.statuses[concept].basis if concept in p.statuses else None)
            for p in periods
            if concept in p.values
        }
        broader = {b for b in bases if b}
        if len(bases) < 2:
            continue
        if len(broader) != 1:
            issues[f"basis:{concept}"] = "Several bases across periods: " + "; ".join(
                sorted(broader)
            )
            continue
        target = next(iter(broader))
        exact = [
            i
            for i, p in enumerate(periods)
            if concept in p.values
            and not measurement_basis(p.statuses[concept].basis if concept in p.statuses else None)
        ]
        if all(
            measurement_basis(alternatives.get((periods[i].period_end, concept), (None, ""))[1])
            == target
            for i in exact
        ):
            for i in exact:
                updates.setdefault(i, {})[concept] = alternatives[(periods[i].period_end, concept)]
        else:
            issues[f"basis:{concept}"] = (
                f"Mixed bases across periods (exact and '{target}'); consistent series unavailable"
            )
    rebuilt = []
    for i, p in enumerate(periods):
        if i not in updates:
            rebuilt.append(p)
            continue
        values, statuses = dict(p.values), dict(p.statuses)
        for concept, (value, basis) in updates[i].items():
            values[concept] = value
            statuses[concept] = ConceptStatus(
                status="reported",
                basis=basis,
                reason="Series harmonized to one basis across periods",
            )
        rebuilt.append(p.model_copy(update={"values": values, "statuses": statuses}))
    return rebuilt[: len(annual)], rebuilt[len(annual) :]


def carry_forward(
    quarters: list[FinancialPeriod], annual: list[FinancialPeriod]
) -> list[FinancialPeriod]:
    """Fill undisclosed quarter-end balances with the latest observation within a year.

    The value keeps its own observation date and a carried_forward status; it is
    never relabelled as a quarter-end measurement. Unresolved or evidenced-absent
    balances are not carried.
    """
    result: list[FinancialPeriod] = []
    for quarter in quarters:
        values, statuses = dict(quarter.values), dict(quarter.statuses)
        for concept in STOCKS:
            status = statuses.get(concept)
            if concept in values or (status is not None and status.status != "not_disclosed"):
                continue
            sources = [
                p.values[concept]
                for p in (*annual, *result)
                if p.period_end < quarter.period_end and concept in p.values
            ]
            sources = [
                v
                for v in sources
                if v.prov.period_end is not None
                and quarter.period_end - v.prov.period_end <= CARRY_WINDOW
            ]
            if not sources:
                continue
            latest = max(sources, key=lambda v: v.prov.period_end or date.min)
            assert latest.prov.period_end is not None
            values[concept] = latest
            statuses[concept] = ConceptStatus(
                status="carried_forward",
                observed_on=latest.prov.period_end,
                reason=f"Not disclosed at {quarter.period_end}; latest observation "
                f"{latest.prov.period_end} carried forward",
            )
        result.append(
            FinancialPeriod(
                kind=quarter.kind,
                fiscal_year=quarter.fiscal_year,
                fiscal_quarter=quarter.fiscal_quarter,
                period_start=quarter.period_start,
                period_end=quarter.period_end,
                values=values,
                statuses=settle(values, statuses),
            )
        )
    return result


def build_history(
    seed: RawFinancials,
    facts: tuple[Fact, ...],
    filings: tuple[Filing, ...],
    *,
    annual_count: int = 5,
    quarter_count: int = 8,
    statement_evidence: tuple[StatementValue, ...] = (),
    extractions: tuple[Extraction, ...] = (),
    source_cik: str | None = None,
) -> tuple[RawFinancials, dict[str, str]]:
    """Latest eligible observations; never trust a comparative fact's fy/fp as its period.

    Calendar anchors come from each filing's own report date and full-year/YTD
    duration. Unsupported fiscal transitions and conflicting observations stay
    explicit. No LLM is invoked per historical period.
    """
    if annual_count < 1 or quarter_count < 4:
        raise ValueError("Require at least one annual period and four quarters")
    cutoff = seed.as_of
    # A successor registrant's history may live under its predecessor's CIK.
    data_cik = source_cik or seed.cik
    facts = tuple(
        f
        for f in facts
        if f.filed <= cutoff
        and f.end <= cutoff
        and f.form in ("10-K", "10-Q", "10-K/A", "10-Q/A")
        and f.namespace == "us-gaap"
    )
    filings = tuple(
        f
        for f in filings
        if f.filed <= cutoff
        and f.report_date is not None
        and f.report_date <= cutoff
        and f.form in ("10-K", "10-Q", "10-K/A", "10-Q/A")
    )
    issues: dict[str, str] = {}
    annual_reports = {f.report_date for f in filings if f.form == "10-K"}
    extractions = tuple(e for e in extractions if e.package.filing.filed <= cutoff)
    if filings:
        latest_end = max(f.report_date for f in filings if f.report_date is not None)
        earliest_end = latest_end - timedelta(
            days=380 * max(annual_count + 2, quarter_count // 4 + 3)
        )
        filings = tuple(
            f for f in filings if f.report_date is not None and f.report_date >= earliest_end
        )
        facts = tuple(f for f in facts if f.end >= earliest_end)

    status_book: dict[tuple[date, date], dict[FinancialConcept, ConceptStatus]] = {}
    # Concepts whose value came from the issuer's statement structure in a package.
    structured: dict[tuple[date, date], set[FinancialConcept]] = {}
    # Broader-basis statement balances kept aside when an exact value was preferred.
    alternatives: dict[tuple[date, FinancialConcept], tuple[Sourced[float], str]] = {}

    def resolve(
        tags: tuple[str, ...], start: date | None, end: date, unit: str = "USD"
    ) -> Sourced[float] | None:
        matching = [
            f
            for f in facts
            if f.tag in tags and f.start == start and f.end == end and f.unit == unit
        ]
        if not matching:
            return None
        latest = max(f.filed for f in matching)
        matching = [f for f in matching if f.filed == latest]
        if len({f.value for f in matching}) != 1:
            raise SECError("Conflicting latest eligible facts/aliases; review required")
        fact = sorted(matching, key=lambda f: (f.accession, f.tag))[-1]
        return fact.sourced(data_cik, cutoff)

    def paired_total(concept: FinancialConcept, tag: str, end: date) -> Sourced[float] | None:
        """A total, or current plus noncurrent, taken from one filing.

        Components are never resolved independently: a later filing that repeats
        only one component as a comparative would otherwise pair values from
        different vintages. Use the latest filing that reports a complete set.
        """
        names = (tag, tag + "Current", tag + "Noncurrent")
        by_accession: dict[str, dict[str, list[Fact]]] = {}
        for fact in facts:
            if fact.tag in names and fact.start is None and fact.end == end and fact.unit == "USD":
                by_accession.setdefault(fact.accession, {}).setdefault(fact.tag, []).append(fact)
        complete = {
            accession: parts
            for accession, parts in by_accession.items()
            if tag in parts or {names[1], names[2]} <= parts.keys()
        }
        if not complete:
            return None
        filed = {
            a: max(f.filed for fs in parts.values() for f in fs) for a, parts in complete.items()
        }
        latest = max(filed.values())
        candidates = sorted(a for a in complete if filed[a] == latest)

        def one(parts: dict[str, list[Fact]], name: str) -> Sourced[float] | None:
            if name not in parts:
                return None
            if len({f.value for f in parts[name]}) != 1:
                raise SECError("Conflicting values for one element within a filing")
            return parts[name][0].sourced(data_cik, cutoff)

        values: list[Sourced[float]] = []
        for accession in candidates:
            parts = complete[accession]
            total, current, noncurrent = (one(parts, name) for name in names)
            if any(v is not None and v.value < 0 for v in (total, current, noncurrent)):
                raise SECError("Negative component/total")
            if current is not None and noncurrent is not None:
                combined = derived(
                    [(1.0, current), (1.0, noncurrent)],
                    end,
                    cutoff,
                    f"{concept}: current plus noncurrent from one filing ({accession}); "
                    "borrowing debt remains separate",
                )
                if total is not None and abs(total.value - combined.value) > 1:
                    raise SECError("Total disagrees with components")
                total = total or combined
            assert total is not None
            values.append(total)
        if len({round(v.value) for v in values}) != 1:
            raise SECError("Conflicting latest eligible filings for this total")
        return values[-1]

    def amounts(start: date, end: date) -> dict[FinancialConcept, Sourced[float]]:
        result: dict[FinancialConcept, Sourced[float]] = {}
        for concept, tags in TAGS.items():
            try:
                value = resolve(
                    tags,
                    None if concept in STOCKS else start,
                    end,
                    "shares" if concept == "shares_outstanding" else "USD",
                )
                if value is not None:
                    if concept not in SIGNED and value.value < 0:
                        raise SECError("Unsupported negative magnitude")
                    if concept == "shares_outstanding" and value.value <= 0:
                        raise SECError("Shares must be positive")
                    result[concept] = value
            except SECError as error:
                issues[f"{start}/{end}:{concept}"] = str(error)
        for concept, tag in TOTAL_COMPONENTS.items():
            try:
                total = paired_total(concept, tag, end)
                if total is not None:
                    result[concept] = total
            except SECError as error:
                issues[f"{start}/{end}:{concept}"] = str(error)
        for concept in {v.concept for v in statement_evidence}:
            options = [
                v
                for v in statement_evidence
                if v.concept == concept
                and v.end == end
                and v.start == (None if concept in STOCKS else start)
                and v.amount.prov.as_of <= cutoff
                and v.amount.prov.filed is not None
                and v.amount.prov.filed <= cutoff
            ]
            if not options or f"{start}/{end}:{concept}" in issues:
                continue
            latest = max(v.amount.prov.filed or date.min for v in options)
            options = [v for v in options if v.amount.prov.filed == latest]
            existing = result.get(concept)
            if (
                existing is not None
                and existing.prov.filed is not None
                and existing.prov.filed > latest
            ):
                continue
            if len({v.amount.value for v in options}) > 1 or (
                existing is not None
                and existing.prov.filed == latest
                and abs(existing.value - options[0].amount.value) > 1
            ):
                issues[f"{start}/{end}:{concept}"] = (
                    "Statement evidence conflicts at latest vintage"
                )
                result.pop(concept, None)
            else:
                result[concept] = options[0].amount
        if "change_in_cash" not in result and f"{start}/{end}:change_in_cash" not in issues:
            try:
                opening = resolve(TAGS["cash"], None, start - DAY)
                closing = result.get("cash")
                if opening is not None and closing is not None:
                    result["change_in_cash"] = derived(
                        [(1.0, closing), (-1.0, opening)],
                        end,
                        cutoff,
                        f"Unrestricted cash and equivalents closing less opening: {start}/{end}; "
                        "not the restricted-cash-inclusive cash-flow reconciliation",
                    )
            except SECError as error:
                issues[f"{start}/{end}:change_in_cash"] = str(error)
        statuses: dict[FinancialConcept, ConceptStatus] = {}
        for concept, (item, filed) in from_packages(start, end).items():
            existing = result.get(concept)
            key = f"{start}/{end}:{concept}"
            if item.status == "reported" and item.value is not None:
                if existing is not None and item.basis and BROADER.search(item.basis):
                    if concept in STOCKS:
                        alternatives[(end, concept)] = (item.value, item.basis)
                    statuses[concept] = ConceptStatus(
                        status="reported",
                        reason=f"Statement line basis differs ({item.basis}); exact value kept",
                    )
                    continue
                if (
                    existing is not None
                    and existing.prov.filed is not None
                    and existing.prov.filed > filed
                ):
                    continue  # A later filing's tagged value supersedes this vintage.
                reason = None
                if existing is not None and abs(existing.value - item.value.value) > 1:
                    reason = (
                        f"Tag-rule value {existing.value:g} differs; issuer statement "
                        "structure used"
                    )
                if key in issues:
                    reason = f"Tag rules: {issues.pop(key)}; issuer statement structure used"
                result[concept] = item.value
                structured.setdefault((start, end), set()).add(concept)
                statuses[concept] = ConceptStatus(
                    status="reported", basis=item.basis, reason=reason
                )
            elif existing is None and key not in issues:
                statuses[concept] = ConceptStatus(
                    status=item.status,
                    reason=item.reason or None,
                )
        status_book[(start, end)] = settle(result, statuses)
        return result

    def from_packages(start: date, end: date) -> dict[FinancialConcept, tuple[ConceptValue, date]]:
        """Each concept from the latest filing whose own statements cover the period."""
        chosen: dict[FinancialConcept, tuple[ConceptValue, date]] = {}
        for extraction in extractions:
            filed = extraction.package.filing.filed
            for period in ((None, end), (start, end)):
                for concept, item in extraction.periods.get(period, {}).items():
                    if (concept in STOCKS) != (period[0] is None):
                        continue
                    prior = chosen.get(concept)
                    if (
                        prior is None
                        or filed > prior[1]
                        or (
                            filed == prior[1]
                            and prior[0].status != "reported"
                            and item.status == "reported"
                        )
                    ):
                        chosen[concept] = (item, filed)
        return chosen

    # One full-year interval per own-report-date 10-K; amendments do not add periods.
    calendar: dict[date, tuple[date, int]] = {}
    for filing in filings:
        if filing.form not in ("10-K", "10-K/A"):
            continue
        candidates = [
            f
            for f in facts
            if f.accession == filing.accession
            and f.end == filing.report_date
            and f.start is not None
            and f.tag in CALENDAR_TAGS
            and 350 <= (f.end - f.start).days + 1 <= 380
        ]
        starts = {f.start for f in candidates}
        years = {f.fiscal_year for f in candidates}
        if len(starts) == len(years) == 1:
            start = candidates[0].start
            assert start is not None and filing.report_date is not None
            entry = (start, candidates[0].fiscal_year)
            if filing.report_date in calendar and calendar[filing.report_date] != entry:
                raise SECError("Conflicting full-year calendar anchors")
            calendar[filing.report_date] = entry
        else:
            issues[f"calendar:{filing.accession}"] = (
                "No unique full-year start/year; fiscal transition may need review"
            )
    for period in seed.annual:
        if period.period_end in calendar and calendar[period.period_end] != (
            period.period_start,
            period.fiscal_year,
        ):
            raise SECError("Seed period disagrees with SEC fiscal calendar")
        calendar[period.period_end] = (period.period_start, period.fiscal_year)
    if not calendar:
        raise SECError("No validated annual calendar anchor for history")
    if len({year for _, year in calendar.values()}) != len(calendar):
        raise SECError("Multiple annual endpoints for one fiscal year; transition needs review")

    # key=(fiscal year, cumulative quarter number); Q4 means the full year.
    cumulative: dict[tuple[int, int], tuple[date, date]] = {
        (year, 4): (start, end) for end, (start, year) in calendar.items()
    }
    for filing in filings:
        if filing.form not in ("10-Q", "10-Q/A"):
            continue
        end = filing.report_date
        assert end is not None
        enclosing = [(s, y) for e, (s, y) in calendar.items() if s <= end < e]
        if not enclosing:
            previous_anchors = sorted((e, y) for e, (_, y) in calendar.items() if e < end)
            if not previous_anchors or (end - previous_anchors[-1][0]).days > 310:
                continue
            enclosing = [(previous_anchors[-1][0] + DAY, previous_anchors[-1][1] + 1)]
        if len(enclosing) != 1:
            issues[f"calendar:{filing.accession}"] = "Overlapping fiscal years"
            continue
        start, year = enclosing[0]
        days = (end - start).days + 1
        quarter = next(
            (
                q
                for q, low, high in ((1, 70, 110), (2, 150, 205), (3, 240, 300))
                if low <= days <= high
            ),
            None,
        )
        if quarter is None or not any(
            f.accession == filing.accession
            and f.start == start
            and f.end == end
            and f.tag in CALENDAR_TAGS
            for f in facts
        ):
            issues[f"calendar:{filing.accession}"] = "No compatible YTD fiscal interval"
            continue
        key = (year, quarter)
        if key in cumulative and cumulative[key] != (start, end):
            raise SECError("Conflicting fiscal-quarter endpoints")
        cumulative[key] = (start, end)

    # Keep enough older cumulative periods to derive Q4 and the oldest retained quarter.
    relevant_years = sorted({y for y, _ in cumulative})[
        -max(annual_count + 1, quarter_count // 4 + 2) :
    ]
    cumulative = {k: v for k, v in cumulative.items() if k[0] in relevant_years}
    totals = {key: amounts(start, end) for key, (start, end) in cumulative.items()}
    # Enriched, reviewed-format seed can contribute concepts lacking standard historical tags.
    for period in seed.annual:
        key = (period.fiscal_year, 4)
        if cumulative.get(key) != (period.period_start, period.period_end):
            continue
        for concept, value in period.values.items():
            existing = totals[key].get(concept)
            if (
                existing is not None
                and abs(existing.value - value.value) > 1
                and concept in structured.get((period.period_start, period.period_end), set())
            ):
                issues[f"seed:{period.period_end}:{concept}"] = (
                    f"Seed value {value.value:g} differs; issuer statement structure kept"
                )
            elif existing is not None and abs(existing.value - value.value) > 1:
                issues[f"seed:{period.period_end}:{concept}"] = (
                    "Seed conflicts with latest eligible XBRL; value excluded"
                )
                totals[key].pop(concept, None)
            elif (
                existing is None
                and f"{period.period_start}/{period.period_end}:{concept}" not in issues
            ):
                totals[key][concept] = value

    annual: list[FinancialPeriod] = []
    quarters: list[FinancialPeriod] = []
    for key in sorted(cumulative):
        year, quarter = key
        start, end = cumulative[key]
        current = totals[key]
        if quarter == 4 and current:
            annual.append(
                FinancialPeriod(
                    kind="annual",
                    fiscal_year=year,
                    period_start=start,
                    period_end=end,
                    values=current,
                    statuses=settle(current, status_book.get((start, end), {})),
                )
            )
        if quarter == 1:
            quarter_start = start
            values = dict(current)
            quarter_statuses = dict(status_book.get((start, end), {}))
        else:
            previous_key = (year, quarter - 1)
            previous_interval = cumulative.get(previous_key)
            if previous_interval is None or previous_interval[0] != start:
                issues[f"quarter:{year}:Q{quarter}"] = (
                    "Prior cumulative period unavailable; quarter not fabricated"
                )
                continue
            quarter_start = previous_interval[1] + DAY
            # 12-12-12-16 week retail calendars give a 16 or 17 week fourth quarter.
            if not 70 <= (end - quarter_start).days + 1 <= 125:
                issues[f"quarter:{year}:Q{quarter}"] = "Unsupported quarter length"
                continue
            # Prefer an explicitly reported standalone quarter over subtraction.
            values = amounts(quarter_start, end)
            quarter_statuses = dict(status_book.get((quarter_start, end), {}))
            previous = totals[previous_key]
            current_status = status_book.get((start, end), {})
            previous_status = status_book.get(previous_interval, {})
            for concept in (set(current) & set(previous) & set(values)) - STOCKS:
                difference = current[concept].value - previous[concept].value
                if abs(values[concept].value - difference) > 1:
                    issues[f"quarter:{year}:Q{quarter}:{concept}:tie_out"] = (
                        "Reported quarter differs from cumulative subtraction; "
                        "reported value retained, vintage/rounding review required"
                    )
            for concept in (set(current) & set(previous)) - STOCKS - set(values):
                if f"{quarter_start}/{end}:{concept}" in issues:
                    # Conflicting direct evidence cannot be bypassed by arithmetic.
                    continue
                value = derived(
                    [(1.0, current[concept]), (-1.0, previous[concept])],
                    end,
                    cutoff,
                    f"Quarter = cumulative {start}/{end} minus {start}/{previous_interval[1]}; "
                    "cross-filing vintage comparability requires review",
                )
                if concept not in SIGNED and value.value < 0:
                    issues[f"quarter:{year}:Q{quarter}:{concept}"] = (
                        "Negative derived magnitude; possible restatement/basis change"
                    )
                    continue
                values[concept] = value
                bases = sorted(
                    {
                        b
                        for book in (current_status, previous_status)
                        if (b := book.get(concept, _NONE).basis)
                    }
                )
                quarter_statuses[concept] = ConceptStatus(
                    status="reported", basis="; ".join(bases) or None
                )
            for concept in (set(current_status) | set(previous_status)) - STOCKS - set(values):
                if concept in quarter_statuses:
                    continue  # Direct evidence about the standalone quarter itself.
                kinds = {
                    book[concept].status if concept in book else "missing"
                    for book in (current_status, previous_status)
                }
                one_side_zero = (
                    concept in current
                    and previous_status.get(concept, _NONE).status == "not_presented"
                ) or (
                    concept in previous
                    and current_status.get(concept, _NONE).status == "not_presented"
                )
                if kinds == {"not_presented"}:
                    quarter_statuses[concept] = ConceptStatus(
                        status="not_presented",
                        reason="Not presented in either cumulative period",
                    )
                elif one_side_zero:
                    # An evidenced absence is a zero: the quarter is the other side alone.
                    parts = (
                        [(1.0, current[concept])]
                        if concept in current
                        else [(-1.0, previous[concept])]
                    )
                    value = derived(
                        parts,
                        end,
                        cutoff,
                        f"Quarter = cumulative {start}/{end} minus {start}/"
                        f"{previous_interval[1]}; the absent side is not presented (zero)",
                    )
                    if concept not in SIGNED and value.value < 0:
                        quarter_statuses[concept] = ConceptStatus(
                            status="unresolved",
                            reason="Negative derived magnitude from a one-sided derivation",
                        )
                        continue
                    values[concept] = value
                    quarter_statuses[concept] = ConceptStatus(
                        status="reported",
                        basis="derived with an evidenced zero cumulative period",
                    )
                elif "unresolved" in kinds or "missing" in kinds:
                    quarter_statuses[concept] = ConceptStatus(
                        status="unresolved",
                        reason="Quarter not derivable: a cumulative period is unresolved",
                    )
                else:
                    quarter_statuses[concept] = ConceptStatus(
                        status="not_disclosed",
                        reason="Quarter not derivable: a cumulative period lacks a value",
                    )
        for period in seed.quarterly:
            if (period.period_start, period.period_end) != (quarter_start, end):
                continue
            for concept, value in period.values.items():
                existing = values.get(concept)
                if existing is not None and abs(existing.value - value.value) > 1:
                    issues[f"seed:{end}:{concept}"] = (
                        "Seed conflicts with historical quarter; excluded"
                    )
                    values.pop(concept, None)
                elif existing is None and f"{quarter_start}/{end}:{concept}" not in issues:
                    values[concept] = value
        if values:
            quarters.append(
                FinancialPeriod(
                    kind="quarterly",
                    fiscal_year=year,
                    fiscal_quarter=quarter,
                    period_start=quarter_start,
                    period_end=end,
                    values=values,
                    statuses=settle(values, quarter_statuses),
                )
            )
    annual, quarters = harmonize(annual, quarters, alternatives, issues)
    quarters = carry_forward(sorted(quarters, key=lambda p: p.period_end), annual)
    annual = sorted(annual, key=lambda p: p.period_end)[-annual_count:]
    quarters = sorted(quarters, key=lambda p: p.period_end)[-quarter_count:]
    for label, periods in (("annual", annual), ("quarterly", quarters)):
        if any(
            a.period_end + DAY != b.period_start
            for a, b in zip(periods[:-1], periods[1:], strict=True)
        ):
            issues[label + "_continuity"] = (
                "Retained periods have gaps; counts alone are insufficient"
            )
    ttm = build_ttm(tuple(quarters), cutoff)
    latest_known_end = max(
        [end for _, end in cumulative.values()]
        + [f.report_date for f in filings if f.report_date is not None]
    )
    if ttm is not None and ttm.period_end != latest_known_end:
        issues["ttm"] = "Latest known reporting period is missing; stale TTM suppressed"
        ttm = None
    if len(annual) < annual_count:
        earlier = [
            d for d in annual_reports if annual and d is not None and d < annual[0].period_end
        ]
        issues["annual_history"] = f"Only {len(annual)}/{annual_count} annual periods available" + (
            "" if earlier else "; the registrant has no earlier annual reports"
        )
    if len(quarters) < quarter_count:
        issues["quarterly_history"] = (
            f"Only {len(quarters)}/{quarter_count} standalone quarters available"
        )
    if ttm is None:
        issues.setdefault("ttm", "Four consecutive quarters with revenue are required")
    for period in (*annual, *quarters, *((ttm,) if ttm else ())):
        for concept in get_args(FinancialConcept):
            status = period.statuses.get(concept)
            label = f"{period.kind}:{period.period_end}:{concept}"
            if status is not None and status.status == "carried_forward":
                issues[label] = f"carried_forward: {status.reason}"
            elif concept not in period.values:
                issues[label] = (
                    f"{status.status}: {status.reason}"
                    if status is not None and status.reason
                    else (status.status if status else "No validated value; not assumed zero")
                )
    if not annual and not quarters:
        raise SECError("No historical periods could be built")
    return RawFinancials(
        cik=seed.cik, as_of=cutoff, annual=tuple(annual), quarterly=tuple(quarters), ttm=ttm
    ), issues
