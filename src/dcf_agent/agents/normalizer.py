"""Normalization Agent: proposes one-off adjustments to EBIT, with evidence.

Candidates come from code, never from the model:

- lines inside operating income on the latest 10-K's income statement whose label
  names an unusual item ("Other operating charges", "Restructuring", "Gain on sale");
- us-gaap one-off elements tagged in the notes or the cash-flow statement
  (restructuring, impairments, litigation, disposal gains and losses, acquisition
  costs). US GAAP puts these inside income from operations when that subtotal is
  presented (ASC 360-10-45, ASC 420-10-45), so they are taken to be inside EBIT.

Amounts by fiscal year (and TTM) come from the tagged facts. Retrieval finds the
filing's own explanation; the local model labels each item recurring or not and
operating or not, with a short reason and the passage it relied on. Code applies
the rules: an item present in 3 or more of the last 5 years is recurring unless the
evidence says otherwise (then the proposal is ambiguous and goes to Gate 1); an
adjustment above 3% of EBIT, low-confidence or ambiguous is reviewed at Gate 1.
Nothing is applied here: approved adjustments enter `engine.normalize`.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from dcf_agent.contracts.accounting import Adjustment, FilingCitation
from dcf_agent.contracts.inputs import FinancialPeriod, RawFinancials
from dcf_agent.contracts.provenance import Confidence, Provenance, Sourced
from dcf_agent.data.extraction import extract
from dcf_agent.data.sec import Fact, Filing, SECClient
from dcf_agent.data.xbrl import XBRLPackage, read_package
from dcf_agent.llm import LLMError, LocalLLM
from dcf_agent.rag.index import FilingIndex, Hit
from dcf_agent.rag.queries import expand

PROMPT_VERSION = "normalizer-v2"
YEARS = 5
RECURRING_YEARS = 3
IMMATERIAL = 0.005  # below 0.5% of |EBIT| in every year: not worth a model call
MATERIAL = 0.03  # of |EBIT|: the Gate 1 threshold, also the minimum excess of a spike
SPIKE = 3.0  # a recurring item's year at this multiple of its typical level is unusual
PASSAGE_CHARS = 1400
Category = Literal["restructuring", "impairment", "litigation", "disposal", "acquisition", "other"]

# Tagged one-off elements (us-gaap); a positive gain element is income, the rest charges.
TAGGED: dict[str, tuple[Category, bool]] = {
    "RestructuringCharges": ("restructuring", False),
    "RestructuringCostsAndAssetImpairmentCharges": ("restructuring", False),
    "RestructuringSettlementAndImpairmentProvisions": ("restructuring", False),
    "SeveranceCosts1": ("restructuring", False),
    "GoodwillImpairmentLoss": ("impairment", False),
    "GoodwillAndIntangibleAssetImpairment": ("impairment", False),
    "ImpairmentOfIntangibleAssetsExcludingGoodwill": ("impairment", False),
    "ImpairmentOfIntangibleAssetsIndefinitelivedExcludingGoodwill": ("impairment", False),
    "ImpairmentOfLongLivedAssetsHeldForUse": ("impairment", False),
    "ImpairmentOfLongLivedAssetsToBeDisposedOf": ("impairment", False),
    "AssetImpairmentCharges": ("impairment", False),
    "LitigationSettlementExpense": ("litigation", False),
    "LossContingencyLossInPeriod": ("litigation", False),
    "GainLossRelatedToLitigationSettlement": ("litigation", True),
    "GainLossOnDispositionOfAssets": ("disposal", True),
    "GainLossOnDispositionOfAssets1": ("disposal", True),
    "GainLossOnSaleOfPropertyPlantEquipment": ("disposal", True),
    "DisposalGroupNotDiscontinuedOperationGainLossOnDisposal": ("disposal", True),
    "BusinessCombinationAcquisitionRelatedCosts": ("acquisition", False),
}
ONE_OFF_LINE = re.compile(
    r"restructur|impair|litigation|settlement|gain|loss(es)? on|divest|dispos|sale of|"
    r"acquisition|transaction|integration|severance|special|unusual|non-?recurring|"
    r"write-?(off|down)|other operating (charge|expense|income|item|cost)",
    re.I,
)
NONOPERATING_LINE = re.compile(
    r"nonoperating|non-operating|interest|equity (in|method)|investment income|before income tax",
    re.I,
)
TERMS: dict[Category, str] = {
    "restructuring": "restructuring charges severance program",
    "impairment": "impairment charge write-down",
    "litigation": "litigation settlement charge",
    "disposal": "gain loss on sale disposal divestiture",
    "acquisition": "acquisition related costs integration",
    "other": "other operating charges items",
}


class Label(BaseModel):
    """The model's reading of one candidate item."""

    model_config = ConfigDict(extra="forbid")
    recurring: bool
    reason: str = Field(max_length=400)
    passage: str = ""  # "P1".."P3", the passage relied on


# A component the filing names inside a line's total ("$760 million related to the
# impairment of our trademark"), and the components that are one-offs.
COMPONENT = re.compile(
    r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(million|billion)\s+"
    r"(?:related to|associated with|due to|for|from)\s+(.+?)"
    r"(?=(?:[,;]\s*(?:and\s+)?|\s+and\s+)(?:\$|partially|offset|in addition)|\.\s|\.$|$)",
    re.I,
)
ONE_OFF_PART = re.compile(
    r"impair|remeasure|contingent consideration|litigation|settlement|divest|dispos|"
    r"sale of|transaction cost|acquisition|write-?(off|down)|indemnif|discontinu",
    re.I,
)
NOT_ONE_OFF_PART = re.compile(r"\bprogram\b|productivity|amortization", re.I)
NEXT_YEAR = re.compile(r"\bIn (?:fiscal )?(?:19|20)\d\d\b")


def named_one_offs(text: str, label: str, total: float) -> tuple[float, str] | None:
    """The one-off components the filing names after a line's total in ``text``."""
    shown = [f"${total / 1e6:,.0f} million"]
    if total >= 1e9:
        shown.append(f"${total / 1e9:,.1f} billion")
    for figure in shown:
        at = text.find(figure)
        while at >= 0:
            before = text[max(0, at - 160) : at].lower()
            if label.lower() in before:
                window = text[at + len(figure) : at + len(figure) + 1800]
                end = NEXT_YEAR.search(window)
                window = window[: end.start()] if end else window
                parts = []
                for amount, scale, what in COMPONENT.findall(window):
                    value = float(amount.replace(",", "")) * (
                        1e9 if scale.lower() == "billion" else 1e6
                    )
                    if ONE_OFF_PART.search(what) and not NOT_ONE_OFF_PART.search(what):
                        parts.append((value, " ".join(what.split())[:120]))
                found = sum(v for v, _ in parts)
                if parts and found <= total + 1e6:
                    return found, "; ".join(f"${v / 1e6:,.0f}M {w}" for v, w in parts)
            at = text.find(figure, at + 1)
    return None


# The charge a filing's text states for a fiscal year ("In fiscal 2026, the Company
# recognized $385 million of ... severance"), by the item's own word.
STATED_WORD: dict[str, str] = {
    "severance": "severance",
    "restructuring": "restructuring",
    "impairment": "impairment",
    "litigation": "litigation|settlement",
}


def stated_charges(text: str, fiscal_year: int, word: str) -> set[float]:
    """Amounts the text states as recognized for the fiscal year for the item's word."""
    verb = r"(?:recogni[sz]ed|recorded|incurred)"
    amount = r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(million|billion)\b"
    patterns = (
        rf"\b[Ii]n (?:fiscal )?(?:year )?{fiscal_year}\b,?[^.]{{0,60}}?{verb}[^.$]{{0,40}}?"
        rf"{amount}[^.]{{0,100}}?(?:{word})",
        rf"{verb}[^.$]{{0,40}}?{amount}[^.]{{0,100}}?(?:{word})[^.]{{0,60}}?"
        rf"\b(?:in )?(?:fiscal )?{fiscal_year}\b",
        rf"\b[Ii]n (?:fiscal )?(?:year )?{fiscal_year}\b,?[^.]{{0,60}}?{verb}[^.$]{{0,20}}?"
        rf"(?:{word})[^.$]{{0,40}}?{amount}",
    )
    found: set[float] = set()
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.I):
            scale = 1e9 if m.group(2).lower() == "billion" else 1e6
            found.add(float(m.group(1).replace(",", "")) * scale)
    return found


# What a filing says when it calls an item a one-off; checked in the cited passage.
ONE_OFF_WORDS = re.compile(
    r"one-time|one time|non-?recurring|unusual|infrequent|not expected to recur|"
    r"discrete|special item|unique",
    re.I,
)


@dataclass
class Candidate:
    concept: str
    label: str
    category: Category
    gain: bool  # a positive amount is income
    on_face: bool
    amounts: dict[date, Sourced[float]] = field(default_factory=dict)  # by period end
    filings: dict[date, tuple[str, date]] = field(default_factory=dict)  # (accession, filed)
    # Periods whose amount comes from the filing's text, not the tag: why.
    stated: dict[date, str] = field(default_factory=dict)


@dataclass
class Decision:
    candidate: Candidate
    years_present: int
    rule_recurring: bool
    label: Label | None
    adjust: bool
    ambiguous: bool
    confidence: Confidence
    rationale: str
    citation: FilingCitation | None
    passage: str | None
    # Periods where a recurring item is unusually large: period end -> typical level.
    spikes: dict[date, float] = field(default_factory=dict)
    # Periods where the filing names the line's one-off components: (sum, description).
    named: dict[date, tuple[float, str]] = field(default_factory=dict)
    named_citation: FilingCitation | None = None
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class Proposal:
    decisions: list[Decision]
    notes: list[str]

    @property
    def adjustments(self) -> list[Adjustment]:
        return [a for d in self.decisions for a in d.adjustments]

    def for_review(self) -> tuple[tuple["AdjustmentProposal", ...], tuple["Flag", ...]]:
        """What Gate 1 shows: each proposed adjustment, and recurring items only flagged."""
        proposals = tuple(
            AdjustmentProposal(adjustment=a, passage=d.passage, review=a.requires_review)
            for d in self.decisions
            for a in d.adjustments
        )
        flags = tuple(
            Flag(item=d.candidate.label, rationale=d.rationale[:600], passage=d.passage)
            for d in self.decisions
            if d.ambiguous and not d.adjustments
        )
        return proposals, flags


class AdjustmentProposal(BaseModel):
    """One proposed adjustment as Gate 1 shows it."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    adjustment: Adjustment
    passage: str | None = None  # the filing passage the reading relied on
    review: bool = True  # above 3% of EBIT, low confidence or ambiguous


class Flag(BaseModel):
    """A recurring item kept in EBIT that the filing may call a one-off."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    item: str
    rationale: str
    passage: str | None = None


def descendants(package: XBRLPackage, root: str) -> dict[str, float]:
    """Income-statement lines under ``root`` and their sign in it (+1 adds, -1 subtracts)."""
    statement = package.statements["income_statement"]
    children: dict[str, list[tuple[str, float]]] = {}
    for arc in statement.calculations:
        children.setdefault(arc.parent, []).append((arc.child, arc.weight))
    found: dict[str, float] = {}
    stack = [(root, 1.0)]
    while stack:
        parent, sign = stack.pop()
        for child, weight in children.get(parent, []):
            if child not in found:
                found[child] = sign * (1.0 if weight > 0 else -1.0)
                stack.append((child, found[child]))
    return found


def face_candidates(package: XBRLPackage, as_of: date) -> tuple[list[Candidate], str]:
    """Unusual lines inside operating income (or, without it, inside pretax income)."""
    if "income_statement" not in package.statements:
        return [], "no income statement in the latest 10-K"
    lines = extract(package, as_of).classifications["income_statement"]
    roots = [c.line.concept for c in lines if c.category == "operating_income"]
    derived = not roots
    if derived:
        roots = [c.line.concept for c in lines if c.category == "pretax_income"]
    if not roots:
        return [], "no operating or pretax income line"
    inside = descendants(package, roots[0])
    found = []
    for c in lines:
        line = c.line
        if line.abstract or line.concept not in inside or c.category not in ("other", "unknown"):
            continue
        if not ONE_OFF_LINE.search(line.label) or NONOPERATING_LINE.search(line.label):
            continue
        if derived and NONOPERATING_LINE.search(line.concept):
            continue
        tag = line.concept.split(":", 1)[-1]
        category = TAGGED[tag][0] if tag in TAGGED else category_of(line.label)
        # A line that adds to operating income makes a positive amount income.
        gain = inside[line.concept] > 0
        found.append(Candidate(line.concept, line.label, category, gain, on_face=True))
    return found, f"operating income from {roots[0]}" + (
        " (derived from pretax)" if derived else ""
    )


def category_of(label: str) -> Category:
    text = label.lower()
    for name, pattern in (
        ("restructuring", r"restructur|severance"),
        ("impairment", r"impair|write-?(off|down)"),
        ("litigation", r"litigation|settlement"),
        ("disposal", r"divest|dispos|sale of|gain|loss(es)? on"),
        ("acquisition", r"acquisition|transaction|integration"),
    ):
        if re.search(pattern, text):
            return name  # type: ignore[return-value]
    return "other"


def annual_fact(facts: Sequence[Fact], concept: str, start: date, end: date) -> Fact | None:
    """The latest-filed fact for exactly this fiscal period."""
    namespace, tag = concept.split(":", 1)
    matches = [
        f
        for f in facts
        if f.namespace == namespace
        and f.tag == tag
        and f.unit == "USD"
        and f.end == end
        and f.start is not None
        and abs((f.start - start).days) <= 7
    ]
    return max(matches, key=lambda f: f.filed, default=None)


def ttm_value(
    facts: Sequence[Fact], concept: str, annual: FinancialPeriod, ttm: FinancialPeriod
) -> tuple[float, Fact] | None:
    """Latest fiscal year plus this year-to-date less last year-to-date."""
    year = annual_fact(facts, concept, annual.period_start, annual.period_end)
    if year is None:
        return None
    if ttm.period_end == annual.period_end:
        return year.value, year
    namespace, tag = concept.split(":", 1)
    own = [f for f in facts if f.namespace == namespace and f.tag == tag and f.unit == "USD"]
    current = [
        f
        for f in own
        if f.end == ttm.period_end and f.start is not None and f.start > annual.period_end
    ]
    if not current:
        return None
    now = max(current, key=lambda f: f.filed)
    length = now.end - (now.start or now.end)
    prior = [
        f
        for f in own
        if f.start is not None
        and abs((f.end - (now.end - timedelta(days=364))).days) <= 10
        and abs(((f.end - f.start) - length).days) <= 10
    ]
    if not prior:
        return None
    before = max(prior, key=lambda f: f.filed)
    return year.value + now.value - before.value, now


def sourced(
    value: float,
    fact: Fact | None,
    period: FinancialPeriod,
    as_of: date,
    note: str,
    accession: str | None = None,
) -> Sourced[float]:
    """A tagged fact's value, or a value derived from one filing (named by its accession)."""
    filing = fact.accession if fact is not None else accession
    return Sourced[float](
        value=value,
        prov=Provenance(
            source="sec_xbrl" if fact is not None else "derived",
            ref=f"accession {filing}" if filing else "normalizer",
            as_of=as_of,
            period_end=period.period_end,
            filed=fact.filed if fact is not None else None,
            confidence="high",
            note=note,
        ),
    )


def spikes(
    candidate: Candidate, periods: Sequence[FinancialPeriod], ttm: FinancialPeriod | None
) -> dict[date, float]:
    """Years far above the item's typical level (median of the other years, absent = 0)."""
    window = {
        p.period_end: abs(candidate.amounts[p.period_end].value)
        if p.period_end in candidate.amounts
        else 0.0
        for p in periods
    }
    latest = periods[-1].period_end
    found: dict[date, float] = {}
    for period in [*periods, *([ttm] if ttm is not None else [])]:
        item = candidate.amounts.get(period.period_end)
        ebit = period.values.get("ebit")
        if item is None or ebit is None:
            continue
        # The TTM overlaps the latest fiscal year, so that year is not its baseline.
        skip = latest if period is ttm else period.period_end
        others = [v for end, v in window.items() if end != skip]
        typical = median(others) if others else 0.0
        excess = abs(item.value) - typical
        if abs(item.value) >= SPIKE * typical and excess >= MATERIAL * abs(ebit.value):
            found[period.period_end] = typical
    return found


def prompt(
    candidate: Candidate,
    hits: Sequence[Hit],
    years: dict[int, float],
    ebit: float,
    unusual: str = "",
) -> str:
    shown = "\n\n".join(
        f"### P{i}: {h.title[:90]}\n{h.text[:PASSAGE_CHARS]}" for i, h in enumerate(hits, 1)
    )
    history = ", ".join(f"FY{y}: {v / 1e6:,.0f}M" for y, v in sorted(years.items()))
    kind = "income" if candidate.gain else "charge"
    return (
        f"{shown}\n\nItem: {candidate.label} ({kind}), {candidate.category}. Amounts by year: "
        f"{history or 'none'}. {unusual}Latest operating income: {ebit / 1e6:,.0f}M.\n"
        "Using the passages above, decide whether this item is a recurring part of the "
        "company's operations (it happens in most years, as a regular cost of doing business) "
        "or a one-off that the filing describes as unusual or not expected to recur. An item "
        "that appears in most years is recurring. Give a reason of one or two sentences based "
        "on the passages, and the passage you relied on (P1, P2 or P3), or an empty passage "
        "if none explains it."
    )


class NormalizationAgent:
    def __init__(self, sec: SECClient, index: FilingIndex, llm: LocalLLM | None) -> None:
        self.sec, self.index, self.llm = sec, index, llm

    def label(
        self,
        candidate: Candidate,
        hits: Sequence[Hit],
        years: dict[int, float],
        ebit: float,
        unusual: str = "",
    ) -> Label | None:
        if self.llm is None or not hits:
            return None
        try:
            return self.llm.generate(
                "normalizer",
                prompt(candidate, hits, years, ebit, unusual),
                Label,
                prompt_version=PROMPT_VERSION,
            ).value
        except LLMError:
            return None

    def propose(
        self,
        raw: RawFinancials,
        source: str,
        annual_filings: Sequence[Filing],  # newest first
        accessions: Sequence[str],
    ) -> Proposal:
        """Proposals for the last five fiscal years and the TTM."""
        as_of = raw.as_of
        notes: list[str] = []
        periods = sorted(raw.annual, key=lambda p: p.period_end)[-YEARS:]
        if not periods:
            return Proposal([], ["no annual periods"])
        package = read_package(self.sec, source, annual_filings[0], as_of)
        face, where = face_candidates(package, as_of)
        notes.append(where)
        facts = self.sec.get_company_facts(source, as_of)
        tagged_names = {f.tag for f in facts if f.namespace == "us-gaap" and f.tag in TAGGED}
        candidates = face + [
            Candidate(f"us-gaap:{tag}", label_of(tag), *TAGGED[tag], on_face=False)
            for tag in sorted(tagged_names)
            if f"us-gaap:{tag}" not in {c.concept for c in face}
        ]
        latest_ebit = periods[-1].values.get("ebit")
        reference = abs(latest_ebit.value) if latest_ebit is not None else 0.0
        decisions = []
        for candidate in candidates:
            for period in periods:
                fact = annual_fact(facts, candidate.concept, period.period_start, period.period_end)
                value = fact.value if fact is not None else None
                if value is None and candidate.concept.split(":")[0] != "us-gaap":
                    value = package.value(candidate.concept, period.period_start, period.period_end)
                if value:
                    candidate.amounts[period.period_end] = sourced(
                        value,
                        fact,
                        period,
                        as_of,
                        f"{candidate.label} ({candidate.concept})",
                        package.filing.accession,
                    )
                    candidate.filings[period.period_end] = (
                        (fact.accession, fact.filed)
                        if fact is not None
                        else (package.filing.accession, package.filing.filed)
                    )
            if raw.ttm is not None and candidate.concept.startswith("us-gaap:"):
                found = ttm_value(facts, candidate.concept, periods[-1], raw.ttm)
                if found is not None and found[0]:
                    candidate.filings[raw.ttm.period_end] = (found[1].accession, found[1].filed)
                    candidate.amounts[raw.ttm.period_end] = sourced(
                        found[0],
                        None,
                        raw.ttm,
                        as_of,
                        f"{candidate.label}: fiscal year + year to date - prior year to date "
                        f"(accession {found[1].accession})",
                        found[1].accession,
                    )
            if not candidate.on_face:
                self.use_stated(candidate, periods, accessions, as_of)
            years = {
                p.fiscal_year: candidate.amounts[p.period_end].value
                for p in periods
                if p.period_end in candidate.amounts
            }
            if not candidate.amounts:
                continue
            if reference and all(
                abs(v.value) < IMMATERIAL * reference for v in candidate.amounts.values()
            ):
                notes.append(f"{candidate.label}: below {IMMATERIAL:.1%} of EBIT in every year")
                continue
            decisions.append(self.decide(candidate, periods, raw, years, reference, accessions))
        drop_nested(decisions, notes)
        return Proposal(decisions, notes)

    def use_stated(
        self,
        candidate: Candidate,
        periods: Sequence[FinancialPeriod],
        accessions: Sequence[str],
        as_of: date,
    ) -> None:
        """Use the charge the filing's text states when it differs from the tagged amount."""
        lowered = candidate.label.lower()
        word = next((w for k, w in STATED_WORD.items() if k in lowered), None)
        if word is None or not accessions:
            return
        marks = ",".join("?" for _ in accessions)
        for period in periods:
            tagged = candidate.amounts.get(period.period_end)
            if tagged is None:
                continue
            year = str(period.fiscal_year)
            rows = self.index.db.execute(
                f"SELECT chunk_id, text FROM chunks_fts WHERE accession IN ({marks}) "
                "AND text LIKE ? AND text LIKE ?",
                (*accessions, f"%{year}%", f"%{word.split('|')[0]}%"),
            ).fetchall()
            amounts: set[float] = set()
            where = None
            for chunk_id, text in rows:
                found = stated_charges(text, period.fiscal_year, word)
                if found and where is None:
                    where = chunk_id
                amounts |= found
            if len(amounts) != 1 or where is None:
                continue  # none, or several different figures: the tag stands
            (amount,) = amounts
            if abs(amount - abs(tagged.value)) <= 0.02 * abs(tagged.value):
                continue
            chunk = self.index.chunk(where)
            url = self.index.db.execute(
                "SELECT url FROM filings WHERE accession = ?", (chunk.accession,)
            ).fetchone()[0]
            note = (
                f"FY{period.fiscal_year}: the filing states ${amount / 1e6:,.0f}M; the tag gives "
                f"${abs(tagged.value) / 1e6:,.0f}M; the stated charge is used"
            )
            candidate.amounts[period.period_end] = Sourced[float](
                value=amount if tagged.value >= 0 else -amount,
                prov=Provenance(
                    source="sec_filing_text",
                    ref=f"{url}#{chunk.anchor or chunk.chunk_id} (accession {chunk.accession})",
                    as_of=as_of,
                    period_end=period.period_end,
                    filed=chunk.filed,
                    confidence="medium",
                    note=note,
                ),
            )
            candidate.filings[period.period_end] = (chunk.accession, chunk.filed)
            candidate.stated[period.period_end] = note

    def components(
        self, candidate: Candidate, accessions: Sequence[str]
    ) -> tuple[dict[date, tuple[float, str]], FilingCitation | None, str | None]:
        """For an income-statement line: the one-off components its filing names, by year."""
        found: dict[date, tuple[float, str]] = {}
        citation = passage = None
        marks = ",".join("?" for _ in accessions)
        for end, value in sorted(candidate.amounts.items()):
            total = abs(value.value)
            rows = self.index.db.execute(
                f"SELECT chunk_id, text FROM chunks_fts WHERE accession IN ({marks}) "
                "AND text LIKE ?",
                (*accessions, f"%${total / 1e6:,.0f} million%"),
            ).fetchall()
            for chunk_id, text in rows:
                named = named_one_offs(text, candidate.label, total)
                if named is not None:
                    found[end] = named
                    if citation is None:
                        chunk = self.index.chunk(chunk_id)
                        citation = FilingCitation(
                            accession=chunk.accession,
                            filed=chunk.filed,
                            section=chunk.section,
                            table_id=chunk.chunk_id,
                        )
                        passage = text[:600]
                    break
        return found, citation, passage

    def decide(
        self,
        candidate: Candidate,
        periods: Sequence[FinancialPeriod],
        raw: RawFinancials,
        years: dict[int, float],
        reference: float,
        accessions: Sequence[str],
    ) -> Decision:
        query = f"{candidate.label} {TERMS[candidate.category]}"
        end = periods[-1].period_end
        hits = self.index.search(expand(query, end), accessions, k=3)
        present = len(years)
        rule = present >= RECURRING_YEARS
        unusual = spikes(candidate, periods, raw.ttm) if rule else {}
        named, named_citation, named_passage = (
            self.components(candidate, accessions)
            if rule and candidate.on_face
            else ({}, None, None)
        )
        unusual = {end: v for end, v in unusual.items() if end not in named}
        shown = "; ".join(
            f"{end:%Y-%m}: {abs(candidate.amounts[end].value) / 1e6:,.0f}M against a typical "
            f"{typical / 1e6:,.0f}M"
            for end, typical in unusual.items()
        )
        label = self.label(
            candidate, hits, years, reference, f"Unusually large in {shown}. " if shown else ""
        )
        hit = None
        cited = re.match(r"\s*P([1-3])\b", label.passage) if label is not None else None
        if cited:  # the first passage named ("P1, P2" -> P1)
            index = int(cited.group(1)) - 1
            hit = hits[index] if index < len(hits) else None
        # The filing itself must call it a one-off for the model to overturn the rule.
        verified = (
            label is not None
            and not label.recurring
            and hit is not None
            and ONE_OFF_WORDS.search(hit.text) is not None
        )
        history = f"Present in {present} of the last {len(periods)} years"
        reason = f" Filing reading: {brief(label.reason)}" if label is not None else ""
        confidence: Confidence = "medium" if hit is not None else "low"
        adjust = ambiguous = False
        if not rule:
            if label is None:
                adjust, confidence = True, "low"
                rationale = f"{history}: not recurring by rule; no reading of the filings"
            elif label.recurring:
                rationale = f"{history}, but the filing reads as a recurring cost.{reason}"
            else:
                adjust = True
                rationale = f"{history}: not recurring.{reason}"
        elif named:
            # A recurring line whose filing names one-off components: those are adjusted.
            adjust, ambiguous = True, False
            listed = "; ".join(f"{end:%Y-%m}: {what}" for end, (_, what) in sorted(named.items()))
            rationale = (
                f"{history} (recurring line), but the filing names one-off components: {listed}."
                + (f" Unusually large otherwise: {shown}." if unusual else "")
            )
        elif unusual:
            # A recurring item in an unusual year: only the excess over its typical level.
            adjust, ambiguous = True, verified
            rationale = (
                f"{history} (recurring), but {shown}; the excess over the typical level "
                f"(median of the other years) is adjusted.{reason}"
            )
        elif verified:
            # A recurring item is never stripped on the model's reading alone (the word
            # check cannot tell whether the passage is about this item): flagged only.
            ambiguous, confidence = True, "low"
            rationale = (
                f"{history} (recurring by rule): kept in EBIT, but the filing may call it a "
                f"one-off ({label.passage if label else ''}); for review at Gate 1.{reason}"
            )
        else:
            rationale = f"{history}: recurring, kept in EBIT.{reason}"
        if candidate.stated:
            rationale += " " + "; ".join(candidate.stated.values()) + "."
        if not (rule and adjust and (unusual or named)):
            unusual, named = {}, {}
        citation = (
            FilingCitation(
                accession=hit.accession,
                filed=self.index.chunk(hit.chunk_id).filed,
                section=hit.section,
                table_id=hit.chunk_id,
            )
            if hit is not None
            else None
        )
        decision = Decision(
            candidate=candidate,
            years_present=present,
            rule_recurring=rule,
            label=label,
            adjust=adjust,
            ambiguous=ambiguous,
            confidence=confidence,
            rationale=rationale,
            citation=citation,
            passage=hit.text[:600] if hit is not None else None,
            spikes=unusual,
            named=named,
            named_citation=named_citation,
        )
        if named and decision.passage is None:
            decision.passage = named_passage
        if adjust:
            decision.adjustments = adjustments(
                decision, [*periods, *([raw.ttm] if raw.ttm else [])], raw.as_of
            )
        return decision


def brief(text: str, sentences: int = 2) -> str:
    """The model's reason cut to its first sentences (the rule and amounts come from code)."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", " ".join(text.split()))
    return " ".join(parts[:sentences])


def label_of(tag: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tag).replace(" 1", "").strip()


def adjustments(
    decision: Decision, periods: Sequence[FinancialPeriod], as_of: date
) -> list[Adjustment]:
    """One EBIT adjustment per period with an amount: charges are added back, gains removed."""
    candidate = decision.candidate
    out = []
    for period in periods:
        item = candidate.amounts.get(period.period_end)
        ebit = period.values.get("ebit")
        if item is None or ebit is None:
            continue
        partial = decision.spikes or decision.named
        if partial and period.period_end not in {**decision.spikes, **decision.named}:
            continue
        income = item.value > 0 if candidate.gain else item.value < 0
        size = abs(item.value)
        if period.period_end in decision.named:
            size, what = decision.named[period.period_end]
            item = item.model_copy(
                update={
                    "prov": item.prov.model_copy(
                        update={
                            "source": "derived",
                            "filed": None,
                            "note": f"{item.prov.note}; one-off components named in the "
                            f"filing: {what}",
                        }
                    )
                }
            )
        elif decision.spikes:
            typical = decision.spikes[period.period_end]
            size -= typical
            item = item.model_copy(
                update={
                    "prov": item.prov.model_copy(
                        update={
                            "source": "derived",
                            "filed": None,
                            "note": f"{item.prov.note}; excess over the typical level "
                            f"{typical:,.0f} (median of the other years)",
                        }
                    )
                }
            )
        accession, filed = candidate.filings[period.period_end]
        evidence = [FilingCitation(accession=accession, filed=filed, section=candidate.concept)]
        for extra in (decision.citation, decision.named_citation):
            if extra is not None and extra not in evidence:
                evidence.append(extra)
        out.append(
            Adjustment(
                adjustment_id=f"{candidate.concept.split(':')[-1]}:{period.kind}:{period.period_end}",
                item=candidate.label,
                period_end=period.period_end,
                as_of=as_of,
                target="ebit",
                amount=item.model_copy(update={"value": size}),
                direction="decrease" if income else "increase",
                rationale=decision.rationale[:600],
                evidence=tuple(evidence),
                reference_ebit=ebit,
                confidence=decision.confidence,
                ambiguous=decision.ambiguous,
            )
        )
    return out


def drop_nested(decisions: list[Decision], notes: list[str]) -> None:
    """A tagged detail inside an adjusted face line is not adjusted twice."""
    face = [d for d in decisions if d.candidate.on_face and d.adjust]
    for d in decisions:
        if d.candidate.on_face or not d.adjust:
            continue
        for line in face:
            adjusted = {a.period_end for a in line.adjustments}
            inside = [
                end
                for end, a in d.candidate.amounts.items()
                if end in adjusted and abs(a.value) <= abs(line.candidate.amounts[end].value) + 1
            ]
            if inside:
                d.adjustments = [a for a in d.adjustments if a.period_end not in inside]
                notes.append(
                    f"{d.candidate.label}: within the adjusted line {line.candidate.label} "
                    f"in {len(inside)} period(s); not adjusted twice"
                )
                if not d.adjustments:
                    d.adjust = False
