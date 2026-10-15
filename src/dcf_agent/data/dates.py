"""Find and parse the valuation date in a message, deterministically.

Supported: ISO and US numeric dates, written dates ("Oct 1, 2026"), "today",
"yesterday", "N days/weeks/months/years ago", quarter and year ends ("end of Q3
2025", "end of 2025", "end of last quarter") and "last fiscal year end" (the
report date of the company's latest 10-K, supplied by the caller). Future dates
are rejected; a weekend or holiday is kept (prices use the last trading day).
"""

import calendar
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

MONTHS = {
    name: i
    for i in range(1, 13)
    for name in (calendar.month_name[i].lower(), calendar.month_abbr[i].lower())
}
MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4}
EARLIEST = date(1994, 1, 1)  # EDGAR electronic filing


@dataclass(frozen=True)
class FoundDate:
    start: int
    end: int
    value: date | None
    source: str  # how the date was obtained, shown to the user
    error: str | None = None


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def quarter_end(year: int, quarter: int) -> date:
    return month_end(year, quarter * 3)


def shift_months(day: date, months: int) -> date:
    total = day.year * 12 + day.month - 1 - months
    year, month = divmod(total, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def _patterns(
    today: date, fiscal_year_end: Callable[[], date | None] | None
) -> list[tuple[re.Pattern[str], Callable[[re.Match[str]], tuple[date | None, str]]]]:
    def month_of(word: str) -> int:
        key = word.lower().rstrip(".")
        return MONTHS.get(key, MONTHS.get(key[:3], 0))

    def amount(word: str) -> int:
        return int(word) if word.isdigit() else NUMBERS[word.lower()]

    def ago(m: re.Match[str]) -> tuple[date | None, str]:
        n, unit = amount(m["n"]), m["unit"].lower()
        if unit.startswith("day"):
            return today - timedelta(days=n), m[0]
        if unit.startswith("week"):
            return today - timedelta(weeks=n), m[0]
        if unit.startswith("month"):
            return shift_months(today, n), m[0]
        return shift_months(today, 12 * n), m[0]

    def last_period(m: re.Match[str]) -> tuple[date | None, str]:
        unit = m["unit"].lower()
        if unit == "month":
            return today.replace(day=1) - timedelta(days=1), m[0]
        if unit == "quarter":
            q = (today.month - 1) // 3
            return (quarter_end(today.year, q) if q else date(today.year - 1, 12, 31)), m[0]
        return date(today.year - 1, 12, 31), m[0]

    def fiscal(m: re.Match[str]) -> tuple[date | None, str]:
        found = fiscal_year_end() if fiscal_year_end else None
        return found, "last fiscal year end (latest 10-K report date)"

    n = r"(?P<n>\d+|a|an|one|two|three|four|five|six)"
    return [
        (
            re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b"),
            lambda m: (date(int(m["y"]), int(m["m"]), int(m["d"])), m[0]),
        ),
        (
            re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})\b"),
            lambda m: (date(int(m["y"]), int(m["m"]), int(m["d"])), m[0]),
        ),
        (
            re.compile(
                rf"(?i)\b(?P<mon>{MONTH})\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})\b"
            ),
            lambda m: (date(int(m["y"]), month_of(m["mon"]), int(m["d"])), m[0]),
        ),
        (
            re.compile(
                rf"(?i)\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon>{MONTH})\s+(?P<y>\d{{4}})\b"
            ),
            lambda m: (date(int(m["y"]), month_of(m["mon"]), int(m["d"])), m[0]),
        ),
        (
            re.compile(
                r"(?i)\blast\s+fiscal\s+year[- ]end\b|\bend\s+of\s+(the\s+)?last\s+fiscal\s+year\b"
            ),
            fiscal,
        ),
        (
            re.compile(
                r"(?i)\b(?:end\s+of\s+(?:the\s+)?)?(?:q(?P<q>[1-4])|(?P<o>first|second|third|fourth)"
                r"\s+quarter)(?:\s+of)?\s+(?P<y>\d{4})\b"
            ),
            lambda m: (
                quarter_end(int(m["y"]), int(m["q"]) if m["q"] else ORDINAL[m["o"].lower()]),
                m[0],
            ),
        ),
        (
            re.compile(
                r"(?i)\b(?:end\s+of\s+(?:the\s+)?last|last)\s+"
                r"(?P<unit>month|quarter|year)(?:[- ]end)?\b"
            ),
            last_period,
        ),
        (
            re.compile(r"(?i)\b(?:end\s+of|year[- ]end(?:\s+of)?)\s+(?P<y>\d{4})\b"),
            lambda m: (date(int(m["y"]), 12, 31), m[0]),
        ),
        (re.compile(rf"(?i)\b{n}\s+(?P<unit>days?|weeks?|months?|years?)\s+ago\b"), ago),
        (re.compile(r"(?i)\byesterday\b"), lambda m: (today - timedelta(days=1), m[0])),
        (re.compile(r"(?i)\btoday\b|\bright now\b"), lambda m: (today, m[0])),
    ]


def find_date(
    text: str, today: date, fiscal_year_end: Callable[[], date | None] | None = None
) -> FoundDate | None:
    """The first date expression in the text (earliest position, then pattern order)."""
    best: FoundDate | None = None
    for pattern, convert in _patterns(today, fiscal_year_end):
        match = pattern.search(text)
        if match is None or (best is not None and match.start() >= best.start):
            continue
        try:
            value, source = convert(match)
        except (ValueError, KeyError):
            best = FoundDate(match.start(), match.end(), None, match[0], "Not a valid date")
            continue
        error = None
        if value is None:
            error = "No 10-K filed yet, so there is no fiscal year end to use"
        elif value > today:
            error = f"{value} is in the future; valuations use information available on the date"
        elif value < EARLIEST:
            error = f"{value} precedes electronic SEC filings"
        best = FoundDate(match.start(), match.end(), value, source, error)
    return best
