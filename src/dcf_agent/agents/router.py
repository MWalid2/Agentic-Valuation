"""Router: turn the first message into a company, a valuation date, an intent and a plan.

Code decides everything it can: the "<company>, <date>[, question]" split, company
resolution, date parsing, scope and intent patterns. The small local model is
asked only to pick the company phrase out of a free-form sentence (and the
phrase must occur in the message) or to label a question no pattern recognizes.
Several plausible companies, a rejected date or an out-of-scope company are
never resolved silently: Gate 0 shows them to the user.
"""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from dcf_agent.contracts.external import InputReadiness
from dcf_agent.contracts.inputs import Intent, ValuationRequest
from dcf_agent.data.companies import Candidate, CompanyIndex, Resolution
from dcf_agent.data.dates import FoundDate, find_date
from dcf_agent.data.scope import assess_scope
from dcf_agent.data.sec import SECClient, SECError
from dcf_agent.llm import LLMError, LocalLLM

PROMPT_VERSION = "router-v2"
INTENTS: dict[Intent, str] = {
    "I1": "data snapshot: retrieve reported figures (revenue, cash, debt, margins)",
    "I2": "historical analysis: trends of margins, growth or returns over several years",
    "I3": "cost of capital: WACC, discount rate, beta, cost of equity or debt",
    "I4": "assumption rationale: why a forecast or valuation assumption was chosen",
    "I5": "scenario: bull, base or bear case drivers and outcomes",
    "I6": "quick valuation: fair or intrinsic value, value per share, under/overvalued",
    "I7": "sensitivity: what happens to value if an input changes by an amount",
    "I8": "comparables: comparison with peer companies or multiples",
    "I9": "full model: build the complete DCF model or Excel workbook",
    "I10": "audit: where an existing number comes from, its filing or derivation",
    "I11": "modify: set, change or update an assumption to a new value",
}
# Ordered: the first matching pattern wins, specific actions before topics.
PATTERNS: tuple[tuple[Intent, str], ...] = (
    ("I11", r"\b(set|change|update|replace|increase|decrease|raise|lower)\b.*\bto\s+-?\d"),
    (
        "I7",
        r"\bwhat if\b|\bsensitiv|\b(rises?|falls?|drops?|increases?|decreases?|goes up|"
        r"goes down)\s+by\b|[+-]\s?\d+(\.\d+)?\s?(%|bp)",
    ),
    (
        "I10",
        r"\bwhere (does|did|do)\b|\bcomes? from\b|\bsource (of|for)\b|\bwhich filing\b|"
        r"\btrace\b|\bprovenance\b|\bhow (was|is|were) .* (calculated|computed|derived)\b|"
        r"\b(taken|derived|sourced|pulled) from\b|\b(which|what) (xbrl )?(element|tag|line)\b",
    ),
    ("I4", r"^\s*why\b|\bwhy did you\b|\brationale\b|\bjustify\b"),
    ("I5", r"\b(bull|bear|base)[- ]case\b|\bscenarios?\b"),
    ("I8", r"\bpeers?\b|\bcomparables?\b|\bcomps\b|\bcompared? (it )?(to|with)\b"),
    ("I9", r"\bexcel\b|\bspreadsheet\b|\bworkbook\b|\bfull (dcf )?model\b|\bbuild the model\b"),
    ("I3", r"\bwacc\b|\bcost of (capital|equity|debt)\b|\bdiscount rate\b|\bbeta\b|\bhurdle"),
    (
        "I6",
        r"\bfair value\b|\bintrinsic\b|\bworth\b|\bvalue per share\b|\b(under|over)valued\b|"
        r"\bprice target\b|\bvaluation\b",
    ),
    (
        "I2",
        r"\btrends?\b|\bover (the )?(last|past) \w+ (years|quarters)\b|"
        r"\b(last|past) (\d+|two|three|four|five|ten) (years|quarters)\b|\bhistor|"
        r"\bevolved?\b|\bsince (19|20)\d\d\b|\bover time\b|\byear[- ]over[- ]year\b|\bcagr\b",
    ),
    (
        "I1",
        r"\b(revenues?|sales|ebit|ebitda|net income|earnings|cash|debt|net debt|capex|"
        r"free cash flow|fcf|margins?|shares|interest expense|eps|assets|liabilities|equity)\b",
    ),
)
RULES = tuple((intent, re.compile(pattern, re.I)) for intent, pattern in PATTERNS)
# Graph nodes per intent; intents outside Phase 2 are classified but not yet run.
PLANS: dict[Intent, tuple[str, ...]] = {
    "I1": ("data", "answer"),
    "I2": ("data", "normalize", "answer"),
    "I3": ("data", "wacc", "answer"),
    "I6": ("data", "wacc", "value", "answer"),
    "I9": ("data", "wacc", "value", "excel", "answer"),
    "I10": ("data", "answer"),
}
QUESTION_WORDS = re.compile(
    r"(?i)^(what|which|how|why|where|who|when|is|are|does|do|can|could|should|would|give|"
    r"show|tell|list|compare|build|set|change|update|value|estimate|calculate)\b"
)


class CompanyChoice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ticker: str
    cik: str
    name: str
    match: str
    other_classes: tuple[str, ...] = ()


class Route(BaseModel):
    """Everything Gate 0 shows; nothing here is final until the user confirms."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str
    status: Literal["resolved", "ambiguous", "not_found"]
    company: CompanyChoice | None = None
    candidates: tuple[CompanyChoice, ...] = ()
    valuation_date: date | None = None
    date_source: str = ""
    date_error: str | None = None
    question: str | None = None
    intent: Intent | None = None
    intent_source: Literal["rule", "model", "none"] = "none"
    extraction: Literal["rules", "model"] = "rules"
    scope: InputReadiness | None = None
    plan: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """Resolved, dated and in scope: Gate 0 only needs a confirmation."""
        return (
            self.status == "resolved"
            and self.valuation_date is not None
            and self.date_error is None
            and self.scope is not None
            and self.scope.status != "blocked"
        )

    def request(self, mode: Literal["info", "quick", "full"] = "info") -> ValuationRequest:
        if not self.ready or self.company is None or self.valuation_date is None:
            raise ValueError("Route is not resolved; confirm at Gate 0 first")
        return ValuationRequest(
            ticker=self.company.ticker,
            cik=self.company.cik,
            company_name=self.company.name,
            valuation_date=self.valuation_date,
            mode=mode,
            intent=self.intent,
            question=self.question or ("Collect inputs" if mode == "info" else None),
        )


class CompanyPhrase(BaseModel):
    # Not nullable: offered "null", the small model gives up on plain sentences. A phrase
    # that does not occur in the message is discarded instead.
    company: str = Field(description="Company name or ticker copied from the message")


class IntentChoice(BaseModel):
    intent: Intent
    rationale: str = Field(max_length=200)


def choice(candidate: Candidate) -> CompanyChoice:
    return CompanyChoice(
        ticker=candidate.ticker,
        cik=candidate.company.cik,
        name=candidate.company.name,
        match=candidate.match,
        other_classes=candidate.other_classes,
    )


def rule_intent(question: str) -> Intent | None:
    for intent, pattern in RULES:
        if pattern.search(question):
            return intent
    return None


class Router:
    def __init__(
        self,
        sec: SECClient,
        today: date,
        llm: LocalLLM | None = None,
        index: CompanyIndex | None = None,
    ) -> None:
        self.sec, self.today, self.llm = sec, today, llm
        self.index = index or CompanyIndex(sec.get_company_list())

    def classify_intent(self, question: str | None) -> tuple[Intent | None, str]:
        """Patterns first; the model chooses from the enum only when none applies."""
        if not question:
            return None, "none"
        found = rule_intent(question)
        if found is not None:
            return found, "rule"
        if self.llm is None:
            return None, "none"
        prompt = (
            "Classify the user's request into one intent. Choose the action asked for, "
            "not the financial noun mentioned.\n"
            + "\n".join(f"{k}: {v}" for k, v in INTENTS.items())
            + f"\nRequest: {question}"
        )
        try:
            reply = self.llm.generate(
                "router", prompt, IntentChoice, prompt_version=PROMPT_VERSION
            ).value
        except LLMError:
            return None, "none"
        return reply.intent, "model"

    def extract_company(self, text: str) -> str | None:
        """The company phrase of a free-form sentence; it must appear in the text."""
        if self.llm is None:
            return None
        prompt = (
            "The user is asking about one publicly listed company. Return that company's "
            f"name or ticker, copied exactly as it appears in the message.\nMessage: {text}"
        )
        try:
            phrase = self.llm.generate(
                "router", prompt, CompanyPhrase, prompt_version=PROMPT_VERSION
            ).value.company
        except LLMError:
            return None
        if not phrase:
            return None
        phrase = re.sub(r"(?i)['’]s?$", "", phrase.strip())
        return phrase if phrase and phrase.lower() in text.lower() else None

    def fiscal_year_end(self, cik: str) -> date | None:
        try:
            annual = [
                f.report_date
                for f in self.sec.get_submissions(cik, self.today)
                if f.form == "10-K" and f.report_date is not None
            ]
        except SECError:
            return None
        return max(annual, default=None)

    def scope(self, cik: str, valuation_date: date | None) -> tuple[InputReadiness, list[str]]:
        notes: list[str] = []
        try:
            readiness = assess_scope(self.sec.get_company_profile(cik), None)
        except SECError as error:
            return (
                InputReadiness(
                    status="blocked",
                    missing={"scope": ("Company profile unavailable: " + str(error),)},
                    review_notes=(),
                ),
                notes,
            )
        if valuation_date is not None:
            try:
                periodic = [
                    f
                    for f in self.sec.get_submissions(cik, valuation_date)
                    if f.form in ("10-K", "10-Q")
                ]
            except SECError:
                periodic = []
                notes.append("Filing list unavailable; history is checked during collection")
            else:
                if not periodic:
                    reasons = (
                        *readiness.missing.get("scope", ()),
                        f"No 10-K or 10-Q filed on or before {valuation_date}",
                    )
                    readiness = InputReadiness(
                        status="blocked", missing={"scope": reasons}, review_notes=()
                    )
                elif not any(f.form == "10-K" for f in periodic):
                    notes.append("No annual report yet on this date; history will be short")
        return readiness, notes

    def split(self, text: str) -> tuple[str | None, FoundDate | None, str | None, str]:
        """(company phrase, date, question, how): rules for "<company>, <date>[, question]"."""
        found = find_date(text, self.today)
        head = text[: found.start] if found else text
        tail = text[found.end :] if found else ""
        if found is not None and head.rstrip().endswith(","):
            company = head.rstrip().rstrip(",").strip()
            if company and "," not in company:
                question = tail.strip().lstrip(",").strip() or None
                return company, found, question, "rules"
        if found is None and "," in text:
            company, _, question = text.partition(",")
            if company.strip() and not QUESTION_WORDS.match(company.strip()):
                return company.strip(), None, question.strip() or None, "rules"
        words = head.split()
        if words and len(words) <= 4 and not tail.strip() and not QUESTION_WORDS.match(head):
            return head.strip(), found, None, "rules"
        # A sentence: the whole message is the question.
        return None, found, text.strip(), "model"

    def route(self, text: str) -> Route:
        company_text, found, question, how = self.split(text)
        if company_text is None:
            company_text = self.extract_company(text)
        resolution = (
            self.index.resolve(company_text) if company_text else Resolution("not_found", ())
        )
        notes: list[str] = []
        best = resolution.best
        status = resolution.status
        if status == "resolved" and best is not None and best.match in ("fuzzy", "prefix"):
            notes.append(f'"{company_text}" matched {best.company.name}; confirm the company')
        if best is not None and best.other_classes and status == "resolved":
            notes.append(
                f"{best.company.name} also lists {', '.join(best.other_classes)}; "
                f"{best.ticker} is used unless you choose another share class"
            )
        cik = best.company.cik if best is not None and status == "resolved" else None
        if found is not None and found.value is None and found.error and cik:
            # "last fiscal year end" needs the company: parse again with its 10-K dates.
            found = find_date(text, self.today, lambda: self.fiscal_year_end(cik))
        valuation_date, date_source, date_error = None, "", None
        if found is None:
            valuation_date, date_source = self.today, "no date given: today assumed"
        else:
            valuation_date, date_source, date_error = found.value, found.source, found.error
            if date_error is not None:
                valuation_date = None
        intent, source = self.classify_intent(question)
        scope = None
        if cik is not None:
            scope, scope_notes = self.scope(cik, valuation_date)
            notes.extend(scope_notes)
        plan = PLANS.get(intent, ()) if intent else ("gate0_menu",)
        if intent and not plan:
            notes.append(f"{intent} ({INTENTS[intent].split(':')[0]}) arrives in a later phase")
        return Route(
            text=text,
            status=status,  # type: ignore[arg-type]
            company=choice(best) if best is not None and status == "resolved" else None,
            candidates=tuple(choice(c) for c in resolution.candidates),
            valuation_date=valuation_date,
            date_source=date_source,
            date_error=date_error,
            question=question,
            intent=intent,
            intent_source=source,  # type: ignore[arg-type]
            extraction=how,  # type: ignore[arg-type]
            scope=scope,
            plan=plan,
            notes=tuple(notes),
        )
