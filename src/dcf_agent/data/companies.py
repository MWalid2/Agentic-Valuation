"""Resolve a company named in free text to SEC registrants, without guessing.

Matching is deterministic: an exact ticker, then the registrant name with legal
suffixes removed (exact, then as a word prefix), then a fuzzy spelling match.
Several close candidates are returned for the user to choose from (Gate 0); one
candidate is still shown for confirmation.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# Trailing words that do not identify a company: legal forms and listing marks ...
LEGAL = {
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "plc",
    "sa",
    "nv",
    "ag",
    "se",
    "lp",
    "llc",
    "new",
    "de",
    "sab",
    "cv",
    "com",
    "the",
}
# ... and organizational words, which can distinguish two registrants ("Target Corp"
# and "Target Group Inc."), so a match that needs them dropped ranks lower.
ORGANIZATIONAL = {"holdings", "holding", "group", "companies"}
EXACT, BASE_NAME, PREFIX, TICKER_TEXT, FUZZY = 1.0, 0.95, 0.85, 0.75, 0.8
TICKER = 1.0
FUZZY_RATIO = 0.85
# Candidates less than this far below the best score are equally plausible.
WINDOW = 0.05


def tokens(name: str, *, organizational: bool = True) -> tuple[str, ...]:
    """Name words without trailing legal forms (and organizational words, by default)."""
    text = name.lower().replace("&", " ").replace("'", "").replace("’", "")
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    removable = LEGAL | ORGANIZATIONAL if organizational else LEGAL
    while words and words[-1] in removable:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return tuple(words)


@dataclass(frozen=True)
class Company:
    cik: str
    name: str
    tickers: tuple[str, ...]  # primary first, as listed by the SEC
    words: tuple[str, ...]  # without legal forms or organizational words
    legal: tuple[str, ...]  # without legal forms only

    @property
    def compact(self) -> str:
        return "".join(self.words)


@dataclass(frozen=True)
class Candidate:
    company: Company
    ticker: str
    score: float
    match: str  # "ticker", "name", "prefix", "ticker_text" or "fuzzy"

    @property
    def other_classes(self) -> tuple[str, ...]:
        """Other listed common-share tickers; preferred and note listings are omitted."""
        return tuple(
            t for t in self.company.tickers if t != self.ticker and re.fullmatch(r"[A-Z]+", t)
        )


@dataclass(frozen=True)
class Resolution:
    status: str  # "resolved", "ambiguous" or "not_found"
    candidates: tuple[Candidate, ...]

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


class CompanyIndex:
    def __init__(self, listings: list[tuple[str, str, str]]) -> None:
        tickers: dict[str, list[str]] = {}
        names: dict[str, str] = {}
        for ticker, cik, name in listings:
            tickers.setdefault(cik, []).append(ticker)
            names.setdefault(cik, name)
        self.companies = {
            cik: Company(
                cik,
                names[cik],
                tuple(found),
                tokens(names[cik]),
                tokens(names[cik], organizational=False),
            )
            for cik, found in tickers.items()
        }
        self.by_ticker = {t: c for c in self.companies.values() for t in c.tickers}

    def resolve(self, text: str) -> Resolution:
        query = text.strip().strip("\"'").strip()
        query = re.sub(r"(?i)['’]s$", "", query)  # possessive
        found: dict[str, Candidate] = {}

        def add(company: Company, ticker: str, score: float, match: str) -> None:
            prior = found.get(company.cik)
            if prior is None or score > prior.score:
                found[company.cik] = Candidate(company, ticker, score, match)

        symbol = query.upper().replace(".", "-")
        if symbol in self.by_ticker:
            # Upper case reads as a ticker; "ford" or "Fox" are more likely names.
            explicit = query.isupper() or query.isdigit()
            add(
                self.by_ticker[symbol],
                symbol,
                TICKER if explicit else TICKER_TEXT,
                "ticker" if explicit else "ticker_text",
            )
        words, legal = tokens(query), tokens(query, organizational=False)
        if words:
            compact = "".join(words)
            for company in self.companies.values():
                primary = company.tickers[0]
                if company.legal == legal or "".join(company.legal) == "".join(legal):
                    add(company, primary, EXACT, "name")
                elif company.words == words or (company.compact == compact and company.words):
                    add(company, primary, BASE_NAME, "name")
                elif company.words[: len(words)] == words:
                    add(company, primary, PREFIX, "prefix")
            if not found or max(c.score for c in found.values()) < PREFIX:
                for company in self.companies.values():
                    if not company.compact:
                        continue
                    matcher = SequenceMatcher(None, compact, company.compact)
                    if matcher.real_quick_ratio() < FUZZY_RATIO or matcher.quick_ratio() < (
                        FUZZY_RATIO
                    ):
                        continue
                    ratio = matcher.ratio()
                    if ratio >= FUZZY_RATIO:
                        add(company, company.tickers[0], FUZZY * ratio, "fuzzy")
        ranked = sorted(found.values(), key=lambda c: (-c.score, c.company.name))
        if not ranked:
            return Resolution("not_found", ())
        close = [c for c in ranked if c.score > ranked[0].score - WINDOW + 1e-9]
        if len(close) == 1:
            return Resolution("resolved", tuple(ranked[:5]))
        return Resolution("ambiguous", tuple(close[:8]))
