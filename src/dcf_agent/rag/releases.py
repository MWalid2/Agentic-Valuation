"""Management guidance from earnings releases (8-K Item 2.02, Exhibit 99.1), point in time.

The releases filed on or before the valuation date are found through the filing
list (item code 2.02) and each filing's own index page (the exhibit's type,
EX-99.1, not its file name). Guidance is read in code, in two forms:

- guidance or outlook tables: a row whose label names a metric, read in its first
  value column (the current guidance when "current" and "prior" are shown);
- sentences with a forward-looking verb ("expects", "now sees", "outlook"), a metric
  and a number.

Each item keeps the release's own words, a parsed range when there is one (a
percentage as a decimal, or dollars), and its citation. Nothing is inferred: a
release without guidance gives no items, and the absence is recorded.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

from lxml import html

from dcf_agent.contracts.accounting import FilingCitation, FilingItem
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.sec import Filing, SECClient, SECError

SAFE_HTML = html.HTMLParser(no_network=True)  # never fetch external resources
EARNINGS = "2.02"
RELEASES = 2  # the latest earnings releases read
METRICS: tuple[tuple[str, str], ...] = (
    ("organic_growth", r"organic (net )?(revenue|sales)"),
    ("revenue_growth", r"\b(net )?(revenue|sales)\b"),
    ("operating_margin", r"operating (income )?margin"),
    ("gross_margin", r"gross margin"),
    ("eps", r"\beps\b|earnings per (diluted )?share"),
    ("tax_rate", r"tax rate"),
    ("capex", r"capital expenditures|\bcapex\b|capital spending"),
    ("free_cash_flow", r"free cash flow"),
    ("operating_income", r"operating (income|profit)"),
)
# A forward-looking statement: a verb form, not the adjective ("expected tariffs").
CUE = re.compile(
    r"\b(expects?|(?:is|are) expected to|anticipates?|now sees?|forecasts?|projects?|"
    r"targets?|reaffirm\w*|raising|raised|lowering|lowered|continues? to expect|guidance|"
    r"outlook)\b",
    re.I,
)
FORWARD = re.compile(
    r"full[- ]year|fiscal (?:year )?20\d\d|for 20\d\d|in 20\d\d|next (?:fiscal )?year|"
    r"guidance|outlook",
    re.I,
)
# Results tables compare periods; guidance tables do not.
RESULTS = re.compile(r"% change|compared to|prior year|three months ended|quarter ended", re.I)
NUMBER = re.compile(r"\d")
RANGE = re.compile(
    r"(?P<lo>-?\d+(?:\.\d+)?)\s*%?\s*(?:to|-|–|—|and)\s*(?P<hi>-?\d+(?:\.\d+)?)\s*%"
    r"|(?P<one>-?\d+(?:\.\d+)?)\s*%",
    re.I,
)
DOLLARS = re.compile(
    r"\$\s?(?P<lo>\d[\d,]*(?:\.\d+)?)\s*(?:(?P<s1>billion|million)\s*)?"
    r"(?:to|-|–)\s*\$?\s?(?P<hi>\d[\d,]*(?:\.\d+)?)\s*(?P<s2>billion|million)"
    r"|\$\s?(?P<one>\d[\d,]*(?:\.\d+)?)\s*(?P<s3>billion|million)",
    re.I,
)


@dataclass(frozen=True)
class Guidance:
    metric: str  # one of METRICS' keys, or "other"
    label: str
    text: str  # the release's own words
    low: float | None  # decimal for percentages, dollars for amounts
    high: float | None
    unit: str  # "pct", "usd" or ""
    accession: str
    filed: date
    url: str


def metric_of(label: str) -> str | None:
    lowered = label.lower()
    return next((key for key, pattern in METRICS if re.search(pattern, lowered)), None)


SPAN = re.compile(r"\b(low|mid|high)[- ](\d{1,2})s?\s*(?:percent|%)", re.I)
SPANS = {"low": (0, 3), "mid": (4, 6), "high": (7, 9)}  # "mid-20 percent": 24% to 26%


def parse_range(text: str) -> tuple[float | None, float | None, str]:
    span = SPAN.search(text)
    if span:
        base = int(span.group(2))
        bottom, top = SPANS[span.group(1).lower()]
        return (base + bottom) / 100, (base + top) / 100, "pct"
    m = RANGE.search(text)
    if m:
        if m.group("one") is not None:
            v = float(m.group("one")) / 100
            return v, v, "pct"
        lo, hi = float(m.group("lo")) / 100, float(m.group("hi")) / 100
        return min(lo, hi), max(lo, hi), "pct"
    d = DOLLARS.search(text)
    if d:

        def amount(number: str, scale: str | None) -> float:
            factor = {"billion": 1e9, "million": 1e6}.get((scale or "").lower(), 1.0)
            return float(number.replace(",", "")) * factor

        if d.group("one") is not None:
            v = amount(d.group("one"), d.group("s3"))
            return v, v, "usd"
        scale = d.group("s2")
        lo, hi = amount(d.group("lo"), d.group("s1") or scale), amount(d.group("hi"), scale)
        return lo, hi, "usd"
    return None, None, ""


def preceding_text(node: Any, limit: int = 400) -> str:
    """The text just before an element (its earlier siblings, then its parents')."""
    parts: list[str] = []
    while node is not None and sum(len(p) for p in parts) < limit:
        for sibling in node.itersiblings(preceding=True):
            if not isinstance(sibling.tag, str):
                continue  # comments and processing instructions carry no text
            parts.append(" ".join(sibling.text_content().split()))
            if sum(len(p) for p in parts) >= limit:
                break
        node = node.getparent()
    return " ".join(reversed(parts))[-limit:]


def lines(document: str) -> tuple[list[str], list[tuple[list[list[str]], str]]]:
    """The release as text lines, and its tables (rows of cells, the text just before)."""
    tree = cast(Any, html.fromstring(document, parser=SAFE_HTML))
    tables = []
    for table in tree.iter("table"):
        rows = [
            [" ".join(c.text_content().split()) for c in tr.iter("td", "th")]
            for tr in table.iter("tr")
        ]
        tables.append(([[c for c in r if c] for r in rows if any(r)], preceding_text(table)))
    # Tables are read above; the prose is the rest. The source's own line breaks are not
    # paragraph breaks: block elements are.
    for table in list(tree.iter("table")):
        table.drop_tree()
    for node in tree.iter():
        if isinstance(node.tag, str):
            node.text = node.text.replace("\n", " ") if node.text else node.text
        node.tail = node.tail.replace("\n", " ") if node.tail else node.tail
    for node in tree.iter("br"):
        node.tail = "\n" + (node.tail or "")
    for node in tree.iter("p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
        node.tail = "\n" + (node.tail or "")
    text = [" ".join(line.split()) for line in tree.text_content().splitlines()]
    return [t for t in text if t], tables


def read_guidance(document: str, filing: Filing, url: str) -> list[Guidance]:
    """The guidance a release states, from its outlook tables and forward-looking sentences."""
    text, tables = lines(document)
    found: list[Guidance] = []
    seen: set[tuple[str, str]] = set()

    def add(metric: str, label: str, words: str) -> None:
        low, high, unit = parse_range(words)
        plain = re.search(r":\s*(\d{1,2}(?:\.\d+)?)\s*$", words)
        if low is None and plain and metric in ("gross_margin", "operating_margin", "tax_rate"):
            # A margin or tax row in an outlook table often omits the % sign.
            low = high = float(plain.group(1)) / 100
            unit = "pct"
        key = (metric, words[:120])
        if key in seen or (low is None and not NUMBER.search(words)):
            return
        seen.add(key)
        found.append(
            Guidance(
                metric, label, words[:400], low, high, unit, filing.accession, filing.filed, url
            )
        )

    for rows, before in tables:
        flat = " ".join(" ".join(r) for r in rows)
        # A guidance table says so inside, or is headed just above it, and does not
        # compare reported periods.
        if not re.search(r"guidance|outlook", flat + " " + before[-120:], re.I):
            continue
        if RESULTS.search(" ".join(" ".join(r) for r in rows[:3])) and not re.search(
            r"guidance|outlook", flat, re.I
        ):
            continue
        for row in rows:
            if len(row) < 2:
                continue
            metric = metric_of(row[0])
            value = next((c for c in row[1:] if NUMBER.search(c)), None)
            if metric and value:
                add(metric, row[0], f"{row[0]}: {value}")
    sentences = [s for line in text for s in re.split(r"(?<=[.;])\s+", line)]
    for sentence in sentences:
        if len(sentence) > 500 or not CUE.search(sentence) or not NUMBER.search(sentence):
            continue
        metric = metric_of(sentence)
        if metric and FORWARD.search(sentence) and parse_range(sentence)[0] is not None:
            add(metric, metric.replace("_", " "), sentence.strip())
    return found


def earnings_releases(
    sec: SECClient, cik: str, as_of: date, count: int = RELEASES
) -> list[tuple[Filing, str, str]]:
    """The latest earnings releases filed by the date: (8-K, exhibit URL, exhibit HTML)."""
    filings = [
        f
        for f in sec.get_submissions(cik, as_of)
        if f.form.startswith("8-K") and EARNINGS in f.items
    ]
    out: list[tuple[Filing, str, str]] = []
    for filing in reversed(filings):
        if len(out) >= count:
            break
        try:
            index = sec.get_filing_document(
                cik,
                filing.model_copy(update={"primary_document": f"{filing.accession}-index.html"}),
                as_of,
            )
            rows = cast(
                list[Any],
                html.fromstring(index, parser=SAFE_HTML).xpath(
                    "//table[contains(@class,'tableFile')]//tr"
                ),
            )
            names = []
            for tr in rows:
                cells = [" ".join(td.text_content().split()) for td in tr.findall("td")]
                if len(cells) >= 4 and cells[3].upper().startswith("EX-99") and cells[2]:
                    names.append(cells[2].split()[0])
            if not names:
                continue
            exhibit = filing.model_copy(update={"primary_document": names[0]})
            document = sec.get_filing_document(cik, exhibit, as_of)
        except SECError:
            continue
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{filing.accession.replace('-', '')}/{names[0]}"
        )
        out.append((filing, url, document))
    return out


def guidance_items(
    sec: SECClient, cik: str, as_of: date
) -> tuple[list[FilingItem], list[Guidance], list[str]]:
    """Guidance from the latest earnings releases, as cited `FilingItem`s."""
    notes: list[str] = []
    releases = earnings_releases(sec, cik, as_of)
    if not releases:
        return [], [], ["no earnings release (8-K Item 2.02 with Exhibit 99.1) found"]
    read: list[Guidance] = []
    for filing, url, document in releases:
        found = read_guidance(document, filing, url)
        if not found:
            notes.append(f"earnings release {filing.accession} ({filing.filed}) states no guidance")
        read += found
    # The latest release's guidance supersedes older statements of the same metric.
    latest: dict[str, Guidance] = {}
    for g in sorted(read, key=lambda g: g.filed, reverse=True):
        latest.setdefault(g.metric, g)
    items = []
    for n, g in enumerate(latest.values(), 1):
        middle = (g.low + g.high) / 2 if g.low is not None and g.high is not None else None
        amount = (
            Sourced[float](
                value=middle,
                prov=Provenance(
                    source="sec_filing_text",
                    ref=f"{g.url} (accession {g.accession})",
                    as_of=as_of,
                    filed=g.filed,
                    confidence="medium",
                    note=f"{g.label}: midpoint of {g.text[:160]}",
                ),
            )
            if middle is not None
            else None
        )
        items.append(
            FilingItem(
                item_id=f"guidance:{g.metric}:{n}",
                description=g.text,
                citation=FilingCitation(
                    accession=g.accession, filed=g.filed, section="EX-99.1 earnings release"
                ),
                confidence="medium",
                amount=amount,
            )
        )
    return items, list(latest.values()), notes


def describe(items: Sequence[FilingItem]) -> str:
    return "; ".join(i.description for i in items) or "no guidance"
