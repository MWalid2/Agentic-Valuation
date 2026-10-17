"""Filings Analyst: read note items from filing passages, point don't type.

For each field the analyst retrieves passages (BM25, query expansion, the
field's notes first), shows them to the model with an ID on every table cell and
on every tagged number in prose, and asks only which cell (or verbatim quote)
answers. Code then reads the value: a tagged cell gives the exact fact (value,
scale, sign, period); an untagged cell or a quote is parsed with the table's
units. Checks decide the confidence:

- the period must be the filing's fiscal year end (not a prior-year column);
- the unit must suit the field (dollars, shares, a rate, dollars per share);
- a consolidated figure is preferred to a breakdown (dimensional) one;
- a second, differently worded prompt must land on the same value
  (self-consistency; the passages come first, so the model reuses its cache);
- when XBRL tags the field, the tagged value is authoritative and a reading
  that differs by more than 2% is flagged.

A value that fails a check is returned with low confidence and the reason, for
Gate 1; it is never silently accepted. When nothing in the first four passages
answers, the next four are read once.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from dcf_agent.contracts.accounting import FilingCitation
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.llm import LLMError, LocalLLM
from dcf_agent.rag.filings import Cell, Chunk, FactRef
from dcf_agent.rag.index import FilingIndex, Hit
from dcf_agent.rag.queries import expand

PROMPT_VERSION = "filings-reader-v2"
Kind = Literal["USD", "shares", "pure", "USD/shares"]
SCALE_WORDS = {"thousand": 1e3, "thousands": 1e3, "million": 1e6, "millions": 1e6,
               "billion": 1e9, "billions": 1e9}  # fmt: skip
PROMPT_CHARS = 11000  # about 2,750 tokens of passages, inside the 4,096-token context
ROW_LIMIT = 45


@dataclass(frozen=True)
class FieldSpec:
    field_id: str  # the us-gaap element that tags it when the filing tags it
    group: str
    question: str
    rephrased: str
    kind: Kind


def spec(group: str, name: str, question: str, rephrased: str, kind: Kind = "USD") -> FieldSpec:
    return FieldSpec("us-gaap:" + name, group, question, rephrased, kind)


FIELDS: tuple[FieldSpec, ...] = (
    spec("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",
         "How much long-term debt principal is scheduled to be repaid in the next twelve months?",
         "In the debt maturity schedule, which amount is due in the first year?"),
    spec("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo",
         "How much long-term debt principal is due in the second year after the balance sheet "
         "date?", "In the debt maturity schedule, which amount is due in the second year?"),
    spec("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree",
         "How much long-term debt principal matures in year three?",
         "In the debt maturity schedule, which amount is due in the third year?"),
    spec("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour",
         "How much long-term debt principal matures in year four?",
         "In the debt maturity schedule, which amount is due in the fourth year?"),
    spec("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive",
         "How much long-term debt principal matures in year five?",
         "In the debt maturity schedule, which amount is due in the fifth year?"),
    spec("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive",
         "How much long-term debt principal matures after five years?",
         "In the debt maturity schedule, which amount is due thereafter (after year five)?"),
    spec("debt", "LongTermDebtFairValue", "What was the fair value of the company's long-term debt?",  # noqa: E501
         "Which number is the estimated fair value of long-term debt at year end?"),
    spec("debt", "LongTermDebtWeightedAverageInterestRate",
         "What was the weighted-average interest rate on long-term debt?",
         "Which rate is the weighted-average interest rate of long-term debt?", "pure"),
    spec("leases", "LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths",
         "What operating lease payments are due in the next twelve months?",
         "In the operating lease maturity schedule, which payment is due in the first year?"),
    spec("leases", "LesseeOperatingLeaseLiabilityPaymentsDueYearTwo",
         "What operating lease payments are due in the second year?",
         "In the operating lease maturity schedule, which payment is due in the second year?"),
    spec("leases", "LesseeOperatingLeaseLiabilityPaymentsDueAfterYearFive",
         "What operating lease payments are due after year five?",
         "In the operating lease maturity schedule, which payment is due thereafter?"),
    spec("leases", "LesseeOperatingLeaseLiabilityPaymentsDue",
         "What are the total undiscounted operating lease payments?",
         "In the operating lease maturity schedule, which number is the total of lease payments "
         "before imputed interest?"),
    spec("leases", "OperatingLeaseWeightedAverageDiscountRatePercent",
         "What weighted-average discount rate is applied to operating leases?",
         "Which rate is the weighted-average discount rate of operating leases?", "pure"),
    spec("leases", "FinanceLeaseWeightedAverageDiscountRatePercent",
         "What weighted-average discount rate is applied to finance leases?",
         "Which rate is the weighted-average discount rate of finance leases?", "pure"),
    spec("leases", "FinanceLeaseLiabilityPaymentsDue",
         "What are the total undiscounted finance lease payments?",
         "In the finance lease maturity schedule, which number is the total of lease payments "
         "before imputed interest?"),
    spec("dilution", "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber",  # noqa: E501
         "How many stock options were outstanding at the end of the year?",
         "Which number is the count of stock options outstanding at year end?", "shares"),
    spec("dilution",
         "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingWeightedAverageExercisePrice",  # noqa: E501
         "What was the weighted-average exercise price of the stock options outstanding?",
         "Which price is the weighted-average exercise price of options outstanding at year end?",
         "USD/shares"),
    spec("dilution",
         "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsNonvestedNumber",  # noqa: E501
         "How many unvested restricted stock units or other non-option awards were outstanding?",
         "Which number is the count of nonvested restricted stock units or awards at year end?",
         "shares"),
    spec("dilution", "AntidilutiveSecuritiesExcludedFromComputationOfEarningsPerShareAmount",
         "How many potentially dilutive securities were excluded from diluted earnings per share?",
         "Which number of shares was excluded from diluted EPS because it was antidilutive?",
         "shares"),
    spec("dilution",
         "EmployeeServiceShareBasedCompensationNonvestedAwardsTotalCompensationCostNotYetRecognized",  # noqa: E501
         "How much stock-based compensation cost has not yet been recognized?",
         "Which amount is the unrecognized compensation cost of nonvested awards?"),
)  # fmt: skip
BY_ID = {f.field_id: f for f in FIELDS}


class Pointer(BaseModel):
    """The model's answer: where the value is, never the value itself."""

    found: bool
    cell: str | None = Field(default=None, description="Cell or number ID such as P2-R4-C1")
    quote: str | None = Field(
        default=None, description="Exact words containing the number, if it has no ID"
    )


class Reading(BaseModel):
    """One field read from a filing, with its evidence and confidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    field_id: str
    status: Literal["found", "not_found"]
    confidence: Literal["high", "low"]
    value: Sourced[float] | None = None
    citation: FilingCitation | None = None
    cell: str | None = None
    fact_id: str | None = None
    reasons: tuple[str, ...] = ()
    method: Literal["xbrl", "rag"] = "rag"
    model_calls: int = 0


@dataclass
class Rendered:
    """The passages as shown to the model, and what each ID points to."""

    text: str
    cells: dict[str, tuple[Chunk, Cell, int]] = field(default_factory=dict)
    numbers: dict[str, tuple[Chunk, FactRef]] = field(default_factory=dict)
    chunks: list[Chunk] = field(default_factory=list)


def render(hits: list[Hit], index: FilingIndex) -> Rendered:
    """Passages with cell IDs (tables) and number IDs (tagged numbers in prose)."""
    out = Rendered(text="")
    parts: list[str] = []
    for p, hit in enumerate(hits, 1):
        chunk = index.chunk(hit.chunk_id)
        out.chunks.append(chunk)
        lines = [f"### Passage P{p}: {chunk.title[:100]} ({chunk.form}, filed {chunk.filed})"]
        if chunk.kind == "table":
            if chunk.units:
                lines.append(f"Units: {chunk.units}")
            caption = next((x for x in chunk.text.splitlines() if x.startswith("Caption: ")), None)
            if caption:
                lines.append(caption)
            for r, row in enumerate(chunk.rows[: chunk.header_rows + ROW_LIMIT]):
                if r < chunk.header_rows:
                    lines.append("Header: " + " | ".join(c.text for c in row))
                    continue
                shown = []
                for c, cell in enumerate(row):
                    if c == 0 and not NUMBER_CELL.match(cell.text):
                        # The row label, never the answer; only a row without a label
                        # (a schedule laid out by year columns) starts with a number.
                        shown.append(cell.text)
                        continue
                    if not cell.facts and not BLANK.sub("", cell.text):
                        continue  # spacer cells only add noise
                    cell_id = f"P{p}-R{r}-C{c}"
                    out.cells[cell_id] = (chunk, cell, r)
                    shown.append(f"[{cell_id}] {cell.text}")
                lines.append(" | ".join(shown))
        else:
            text = chunk.text
            cursor = 0  # facts follow the document's order: each is placed after the last
            for n, fact in enumerate(chunk.facts, 1):
                number_id = f"P{p}-N{n}"
                pattern = r"\$?\s?(?<![\d.,])" + re.escape(fact.shown) + r"(?![\d])"
                found = re.compile(pattern).search(text, cursor) if fact.shown else None
                if found:
                    start, end = found.span()
                    text = f"{text[:start]}[{number_id}] {text[start:end]}{text[end:]}"
                    cursor = end + len(number_id) + 3
                    out.numbers[number_id] = (chunk, fact)
            lines.append(text)
        parts.append("\n".join(lines))
    text = "\n\n".join(parts)
    out.text = text[:PROMPT_CHARS]
    return out


def instruction(question: str, period_end: date) -> str:
    y = period_end.year
    return (
        "\n\nRead the passages above. Answer the question by pointing to the table cell ID "
        "(like P2-R4-C1) or the number ID in text (like P1-N3) that holds the answer, for the "
        f"whole company (not a segment) at the fiscal year end {period_end:%B %d, %Y}: columns "
        f"headed {y} are that year. In maturity schedules the next twelve months is {y + 1}, "
        f"year two is {y + 2}, year three {y + 3}, year four {y + 4}, year five {y + 5}, and "
        "after year five is 'thereafter'. If the number has no ID, copy the exact words "
        "containing it as the quote. Set found to false only if no passage holds the answer."
        f"\nQuestion: {question}"
    )


ID = re.compile(r"P\d+-(R\d+-C\d+|N\d+)")
NUMBER_CELL = re.compile(r"^\s*\(?-?\$?\s*(\d[\d,]*(\.\d+)?|[\-–—]+)\s*%?\)?\s*$")
BLANK = re.compile(r"[\s​ $%()\-–—]+")


def checking(question: str) -> str:
    return (
        f"{question} Check carefully that both the row label and the column header match "
        "the question (the right kind of item, the latest year, the whole company)."
    )


def parse_number(text: str) -> float | None:
    match = re.search(r"\(?-?\$?\s*(\d[\d,]*(?:\.\d+)?)\s*%?\)?", text)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    negative = "(" in match.group(0) and ")" in match.group(0) or "-" in match.group(0)
    if "%" in text:
        number /= 100
    return -number if negative else number


class FilingsAnalyst:
    def __init__(self, index: FilingIndex, llm: LocalLLM | None) -> None:
        self.index, self.llm = index, llm

    # Deterministic path ---------------------------------------------------------------

    def tagged(self, spec_: FieldSpec, accession: str, period_end: date) -> Reading | None:
        """The XBRL-tagged fiscal-year-end value of the field, when the filing tags it."""
        rows = self.index.db.execute(
            "SELECT chunk_id FROM chunks WHERE accession = ?", (accession,)
        ).fetchall()
        found: list[tuple[Chunk, FactRef]] = []
        for (chunk_id,) in rows:
            chunk = self.index.chunk(chunk_id)
            for fact in chunk.facts:
                if fact.concept == spec_.field_id and is_year_end(fact, period_end):
                    found.append((chunk, fact))
        values = {round(f.value or 0.0, 6) for _, f in found}
        if not found or len(values) != 1:
            return None
        chunk, fact = found[0]
        return self.reading(spec_, chunk, fact.value or 0.0, fact, "xbrl", "high", ())

    # Reading path ----------------------------------------------------------------------

    def ask(self, rendered: Rendered, question: str, period_end: date) -> Pointer | None:
        if self.llm is None:
            return None
        try:
            return self.llm.generate(
                "filings",
                rendered.text + instruction(question, period_end),
                Pointer,
                prompt_version=PROMPT_VERSION,
            ).value
        except LLMError:
            return None

    def resolve(
        self, pointer: Pointer | None, rendered: Rendered, spec_: FieldSpec, period_end: date
    ) -> tuple[float, Chunk, FactRef | None, str, list[str]] | None:
        """(value, chunk, fact, cell ID, problems) for a pointer, or None if unusable."""
        if pointer is None or not pointer.found:
            return None
        cell_id = (pointer.cell or "").strip().strip("[]")
        quote = (pointer.quote or "").strip()
        if not cell_id and ID.fullmatch(quote.strip("[]")):
            cell_id, quote = quote.strip("[]"), ""  # an ID given as the quote
        if cell_id not in rendered.cells and cell_id not in rendered.numbers and quote:
            # A quoted number that is exactly one table cell is that cell (exact scale).
            target = parse_number(quote)
            matches = [
                cid
                for cid, (_, cell, _) in rendered.cells.items()
                if target is not None and parse_number(cell.text) == target
            ]
            if len(matches) == 1:
                cell_id = matches[0]
            else:
                # A quoted number that is a tagged number in the text is that number,
                # preferably the one for the fiscal year end.
                numbers = [
                    (nid, f)
                    for nid, (_, f) in rendered.numbers.items()
                    if target is not None and parse_number(f.shown) == target
                ]
                numbers.sort(key=lambda item: item[1].end != period_end)
                if numbers:
                    cell_id = numbers[0][0]
        problems: list[str] = []
        if cell_id in rendered.cells:
            chunk, cell, row = rendered.cells[cell_id]
            facts = [f for f in cell.facts if f.value is not None]
            if facts:
                fact = next((f for f in facts if not f.dimensional), facts[0])
                if fact.end != period_end:
                    # Right row, other column: move to the fiscal-year-end cell of the same
                    # row, using each cell's own period (no field concept involved).
                    moved = year_end_cell(chunk, row, fact, period_end)
                    if moved is not None:
                        fact, cell_id = moved[0], f"{cell_id.rsplit('-C', 1)[0]}-C{moved[1]}"
                        problems.append("column corrected to the fiscal year end")
                problems += fact_problems(fact, spec_, period_end)
                corrected = [p for p in problems if p != "column corrected to the fiscal year end"]
                return fact.value or 0.0, chunk, fact, cell_id, corrected
            value = parse_number(cell.text)
            if value is None:
                return None
            value *= unit_scale(chunk.units or "", spec_)
            if not header_has_year(chunk, int(cell_id.rsplit("C", 1)[-1]), period_end):
                problems.append("column date not confirmed as the fiscal year end")
            return value, chunk, None, cell_id, problems + ["untagged cell"]
        if cell_id in rendered.numbers:
            chunk, fact = rendered.numbers[cell_id]
            if fact.end != period_end:
                # "3, 3 and 8 million in 2025, 2024 and 2023, respectively": the same
                # element's number for the fiscal year end, from its own tagged period.
                same_year = next(
                    (
                        (nid, f)
                        for nid, (ch, f) in rendered.numbers.items()
                        if ch.chunk_id == chunk.chunk_id
                        and f.concept == fact.concept
                        and f.end == period_end
                        and not f.dimensional
                        and f.unit == fact.unit
                        and (f.start is None) == (fact.start is None)
                    ),
                    None,
                )
                if same_year is not None:
                    cell_id, fact = same_year
            problems += fact_problems(fact, spec_, period_end)
            return fact.value or 0.0, chunk, fact, cell_id, problems
        if quote and len(quote) > 3:
            for chunk in rendered.chunks:
                if quote in chunk.text:  # verbatim only: a paraphrase is rejected
                    value = parse_number(quote)
                    if value is None:
                        return None
                    words = [s for w, s in SCALE_WORDS.items() if re.search(rf"\b{w}\b", quote)]
                    table = unit_scale(chunk.units or "", spec_) if chunk.kind == "table" else 1.0
                    value *= words[0] if words else table
                    if spec_.kind in ("USD", "shares") and abs(value) < 1000:
                        return None  # a ratio, a duration or a price, not an amount or count
                    return value, chunk, None, "quote", ["read from a quote, not a tagged cell"]
        return None

    def read(
        self, spec_: FieldSpec, accessions: list[str], period_end: date, *, use_xbrl: bool = True
    ) -> Reading:
        """Read one field; the XBRL-tagged value first when allowed."""
        if use_xbrl:
            for accession in accessions:
                tagged = self.tagged(spec_, accession, period_end)
                if tagged is not None:
                    return tagged
        query = expand(spec_.question, period_end)
        calls = 0
        # Year-end schedules live in the annual report: read the 10-K first, then the
        # passages of every filing if it does not answer.
        annual = [
            a
            for (a,) in self.index.db.execute(
                f"SELECT accession FROM filings WHERE form = '10-K' AND accession IN "
                f"({','.join('?' for _ in accessions)})",
                tuple(accessions),
            )
        ]
        rounds = [(annual, 0), (accessions, 0)] if annual else [(accessions, 0), (accessions, 4)]
        seen: set[str] = set()
        for scope, offset in rounds:
            hits = [
                h
                for h in self.index.search(query, scope, k=offset + 8, field=spec_.group)[offset:]
                if h.chunk_id not in seen
            ][:4]
            seen |= {h.chunk_id for h in hits}
            if not hits:
                break
            rendered = render(hits, self.index)
            schedule = schedule_reading(rendered, spec_, period_end)
            first = self.resolve(
                self.ask(rendered, spec_.question, period_end), rendered, spec_, period_end
            )
            calls += 1
            if first is None:
                if schedule is not None:  # the model found nothing; the schedule row did
                    value, chunk, fact, cell_id, problems = schedule
                    reason = "read from the schedule row for the year; the model found nothing"
                    return self.reading(
                        spec_, chunk, value, fact, "rag", "low", (reason,), cell_id, calls
                    )
                continue  # nothing here: read the next passages once
            second = self.resolve(
                self.ask(rendered, spec_.rephrased, period_end), rendered, spec_, period_end
            )
            calls += 1
            value, chunk, fact, cell_id, problems = first
            if second is None or not close(second[0], value):
                # Disagreement: a third, careful reading; the majority is kept.
                third = self.resolve(
                    self.ask(rendered, checking(spec_.question), period_end),
                    rendered,
                    spec_,
                    period_end,
                )
                calls += 1
                if third is not None and close(third[0], value):
                    problems = [*problems]
                elif third is not None and second is not None and close(third[0], second[0]):
                    value, chunk, fact, cell_id, problems = second
                    problems = [*problems, "the first reading was outvoted"]
                else:
                    problems = [*problems, "readings disagree"]
            if schedule is not None and problems and not close(schedule[0], value):
                # A flagged reading the fiscal year's row contradicts: the schedule decides.
                # A clean reading is kept (the row search can find another table's year).
                value, chunk, fact, cell_id, _ = schedule
                problems = ["read from the schedule row for the year; the model pointed elsewhere"]
            confidence: Literal["high", "low"] = "low" if problems else "high"
            return self.reading(
                spec_, chunk, value, fact, "rag", confidence, tuple(problems), cell_id, calls
            )
        return Reading(
            field_id=spec_.field_id,
            status="not_found",
            confidence="low",
            reasons=("no passage answered",),
            model_calls=calls,
        )

    def reading(
        self,
        spec_: FieldSpec,
        chunk: Chunk,
        value: float,
        fact: FactRef | None,
        method: Literal["xbrl", "rag"],
        confidence: Literal["high", "low"],
        reasons: tuple[str, ...],
        cell_id: str | None = None,
        calls: int = 0,
    ) -> Reading:
        url = self.index.db.execute(
            "SELECT url FROM filings WHERE accession = ?", (chunk.accession,)
        ).fetchone()[0]
        anchor = fact.fact_id if fact is not None else chunk.anchor
        return Reading(
            field_id=spec_.field_id,
            status="found",
            confidence=confidence,
            value=Sourced[float](
                value=value,
                prov=Provenance(
                    source="sec_xbrl" if method == "xbrl" else "sec_filing_text",
                    ref=f"{url}#{anchor}" if anchor else url,
                    as_of=chunk.filed,
                    period_end=fact.end if fact is not None else chunk.period_end,
                    filed=chunk.filed,
                    confidence="high" if confidence == "high" else "low",
                    note=f"{chunk.title[:80]}; {cell_id or 'tagged fact'}"
                    + (f"; {fact.concept}" if fact is not None else ""),
                ),
            ),
            citation=FilingCitation(
                accession=chunk.accession,
                filed=chunk.filed,
                section=chunk.section,
                table_id=chunk.chunk_id,
            ),
            cell=cell_id,
            fact_id=fact.fact_id if fact is not None else None,
            reasons=reasons,
            method=method,
            model_calls=calls,
        )


def is_year_end(fact: FactRef, period_end: date) -> bool:
    if fact.dimensional or fact.end != period_end or fact.value is None:
        return False
    return fact.start is None or (period_end - fact.start) > timedelta(days=340)


def fact_problems(fact: FactRef, spec_: FieldSpec, period_end: date) -> list[str]:
    problems = []
    if fact.end != period_end:
        problems.append(f"period {fact.end} is not the fiscal year end {period_end}")
    if fact.dimensional:
        problems.append("a breakdown (dimensional) figure, not the consolidated one")
    if fact.unit and fact.unit != spec_.kind:
        problems.append(f"unit {fact.unit}, expected {spec_.kind}")
    # The tag states what the number is: a different standard element is another item
    # (for example next year's lease payment read for the second year's).
    if fact.concept != spec_.field_id and fact.concept.startswith("us-gaap:"):
        problems.append(f"the cell is tagged as {fact.concept.split(':', 1)[1]}")
    return problems


def unit_scale(units: str, spec_: FieldSpec) -> float:
    if spec_.kind == "pure":
        return 1.0
    for word, scale in SCALE_WORDS.items():
        if re.search(rf"\b{word}\b", units.lower()):
            return scale
    return 1.0


def header_has_year(chunk: Chunk, column: int, period_end: date) -> bool:
    for row in chunk.rows[: chunk.header_rows]:
        if column < len(row) and str(period_end.year) in row[column].text:
            return True
    return False


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-9, 0.005 * max(abs(a), abs(b)))


# Maturity-schedule fields: the row a value sits in is fixed by the fiscal year.
SCHEDULE: tuple[tuple[str, int | None], ...] = (
    ("AfterYearFive", None),
    ("NextTwelveMonths", 1),
    ("YearTwo", 2),
    ("YearThree", 3),
    ("YearFour", 4),
    ("YearFive", 5),
)
DASH = re.compile(r"^\s*[\-\u2013\u2014]+\s*$")
Found = tuple[float, Chunk, FactRef | None, str, list[str]]


def schedule_row(spec_: FieldSpec, period_end: date) -> tuple[re.Pattern[str], int | None] | None:
    """The row label of a maturity-schedule field (its calendar year, or "thereafter")."""
    for key, offset in SCHEDULE:
        if spec_.field_id.endswith(key):
            if offset is None:
                return re.compile(r"^\s*(thereafter|after\b|later years)", re.I), None
            year = period_end.year + offset
            return re.compile(rf"^\s*(fiscal\s+(year\s+)?)?{year}\b", re.I), year
    return None


def schedule_kind(spec_: FieldSpec) -> tuple[re.Pattern[str], re.Pattern[str] | None]:
    """(what the table is about, which column) for a schedule field."""
    name = spec_.field_id
    if "OperatingLease" in name:
        return re.compile(r"lease", re.I), re.compile(r"operating", re.I)
    if "FinanceLease" in name:
        return re.compile(r"lease", re.I), re.compile(r"financ", re.I)
    return re.compile(r"debt|borrowing|notes|principal|maturit", re.I), None


def by_year_column(
    chunk: Chunk, p: int, year_label: re.Pattern[str], spec_: FieldSpec
) -> list[Found]:
    """A schedule laid out by year columns: the year's column in its one row of amounts."""
    out: list[Found] = []
    for h, header in enumerate(chunk.rows[: max(chunk.header_rows, 1)]):
        texts = [x.text.strip() for x in header]
        columns = [i for i, t in enumerate(texts) if t and year_label.search(t)]
        years = [t for t in texts if re.fullmatch(r"(19|20)\d\d|thereafter", t, re.I)]
        if len(columns) != 1 or len(years) < 3:
            continue
        rows = [
            (r, cells)
            for r, cells in enumerate(chunk.rows)
            if r > h
            and len(cells) in (len(header), len(header) + 1)
            and sum(parse_number(x.text) is not None or bool(DASH.match(x.text)) for x in cells)
            >= 3
        ]
        if len(rows) != 1:
            continue  # several rows of amounts: which one is the field is not known
        r, cells = rows[0]
        c = columns[0] + (len(cells) - len(header))
        cell = cells[c]
        fact = next((f for f in cell.facts if f.value is not None and not f.dimensional), None)
        if fact is not None:
            value = fact.value or 0.0
        elif DASH.match(cell.text):
            value = 0.0
        else:
            number = parse_number(cell.text)
            if number is None:
                continue
            value = number * unit_scale(chunk.units or "", spec_)
        out.append((value, chunk, fact, f"P{p}-R{r}-C{c}", []))
    return out


def schedule_reading(rendered: Rendered, spec_: FieldSpec, period_end: date) -> Found | None:
    """The value in the fiscal year's row of a maturity schedule, when exactly one is shown.

    Tables: the row whose label is the year (or "thereafter"); the column is the one
    tagged or headed for the field's kind (operating or finance leases), or the only
    number in the row. A dash is zero. Prose schedules ("2029 - $6,556") pair the year
    with the tagged number after it.
    """
    schedule = schedule_row(spec_, period_end)
    if schedule is None:
        return None
    row_label, year = schedule
    about, column = schedule_kind(spec_)
    found: list[Found] = []
    for p, chunk in enumerate(rendered.chunks, 1):
        if chunk.kind != "table" or not about.search(chunk.title + " " + chunk.text[:400]):
            continue
        found += by_year_column(chunk, p, row_label, spec_)
        for r, cells in enumerate(chunk.rows):
            if r < chunk.header_rows or not cells or not row_label.search(cells[0].text):
                continue
            # A dash is a nil amount; blanks and spacers are not numbers.
            numbers = [
                i
                for i, x in enumerate(cells)
                if i > 0 and (parse_number(x.text) is not None or DASH.match(x.text))
            ]
            for c in numbers:
                cell = cells[c]
                if column is not None:
                    tagged = any(column.search(f.concept) for f in cell.facts)
                    headed = any(
                        c < len(h) and column.search(h[c].text)
                        for h in chunk.rows[: chunk.header_rows]
                    )
                    if not (tagged or headed or len(numbers) == 1):
                        continue
                elif len(numbers) > 1 and not any(
                    f.concept.endswith(spec_.field_id.split(":")[-1]) for f in cell.facts
                ):
                    continue  # several numbers and nothing says which: no reading
                fact = next(
                    (f for f in cell.facts if f.value is not None and not f.dimensional), None
                )
                if fact is not None:
                    value = fact.value or 0.0
                elif DASH.match(cell.text):
                    value = 0.0
                else:
                    number = parse_number(cell.text)
                    if number is None:
                        continue
                    value = number * unit_scale(chunk.units or "", spec_)
                found.append((value, chunk, fact, f"P{p}-R{r}-C{c}", []))
    if year is not None:
        for chunk in rendered.chunks:
            if chunk.kind == "table" or not about.search(chunk.text):
                continue
            for m in re.finditer(
                rf"\b{year}\s*[\u2013\u2014-]+\s*\$?\s*(\d{{1,3}}(?:,\d{{3}})*(?:\.\d+)?)",
                chunk.text,
            ):
                fact = next((f for f in chunk.facts if f.shown == m.group(1) and f.value), None)
                if fact is not None:
                    number_id = next(
                        (k for k, (ch, f) in rendered.numbers.items() if f.fact_id == fact.fact_id),
                        "prose",
                    )
                    found.append((fact.value or 0.0, chunk, fact, number_id, []))
    values = {round(v, 2) for v, *_ in found}
    return found[0] if len(values) == 1 else None


def year_end_cell(
    chunk: Chunk, row: int, picked: FactRef, period_end: date
) -> tuple[FactRef, int] | None:
    """The cell of the same row holding the fiscal-year-end value of the same kind."""
    for column, cell in enumerate(chunk.rows[row]):
        for fact in cell.facts:
            if (
                fact.end == period_end
                and not fact.dimensional
                and fact.value is not None
                and fact.unit == picked.unit
                and (fact.start is None) == (picked.start is None)
            ):
                return fact, column
    return None
