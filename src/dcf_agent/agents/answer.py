"""Answers built from templates and sourced values: data (I1), history (I2), cost of
capital (I3) and audit (I10); filing details (dilution, leases, tax rate, captive
finance) and normalized EBIT with the approved adjustments and their passages.

Every number in an answer body is a cited figure: the formatted value followed by a
marker [n] that points to a source entry (source, link, filing date, element,
basis and derivation steps). No model writes text or numbers here; the model may
only choose which concepts a question asks for, from the concept list, when the
keyword rules do not recognize any.
"""

import re
from collections.abc import Callable
from datetime import date
from typing import Any, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field

from dcf_agent.contracts.accounting import Adjustment
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import (
    ConceptStatus,
    FinancialConcept,
    FinancialPeriod,
    Intent,
    ValuationRequest,
)
from dcf_agent.contracts.modeling import ForecastAssumptions, ForecastScenarios, WACCResult
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.llm import LLMError, LocalLLM

PROMPT_VERSION = "answer-concepts-v1"
Unit = Literal["usd", "pct", "ratio", "shares"]
Derived = Literal["free_cash_flow", "net_debt", "gross_margin", "operating_margin", "net_margin"]
Concept = FinancialConcept | Derived
STOCKS: set[str] = {
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
    "net_debt",
}
LABELS: dict[str, str] = {
    "revenue": "Revenue",
    "cogs": "Cost of revenue",
    "sga": "SG&A",
    "rnd": "R&D",
    "ebit": "Operating income (EBIT)",
    "depreciation_amortization": "Depreciation and amortization",
    "stock_based_compensation": "Stock-based compensation",
    "interest_expense": "Interest expense",
    "income_tax_expense": "Income tax expense",
    "pretax_income": "Pre-tax income",
    "net_income": "Net income",
    "cash": "Cash and equivalents",
    "marketable_securities": "Marketable securities",
    "receivables": "Receivables",
    "inventory": "Inventory",
    "payables": "Payables",
    "net_ppe": "Net PP&E",
    "goodwill": "Goodwill",
    "debt": "Debt",
    "operating_lease_liabilities": "Operating lease liabilities",
    "finance_lease_liabilities": "Finance lease liabilities",
    "equity": "Shareholders' equity",
    "minority_interest": "Minority interest",
    "preferred_equity": "Preferred equity",
    "temporary_equity": "Temporary equity",
    "captive_finance_debt": "Captive-finance debt",
    "total_assets": "Total assets",
    "total_liabilities": "Total liabilities",
    "operating_cash_flow": "Operating cash flow",
    "capex": "Capital expenditure",
    "buybacks": "Share buybacks",
    "dividends": "Dividends paid",
    "change_in_cash": "Change in cash",
    "shares_outstanding": "Shares outstanding",
    "free_cash_flow": "Free cash flow (operating cash flow less capex)",
    "net_debt": "Net debt (debt less cash and marketable securities)",
    "gross_margin": "Gross margin",
    "operating_margin": "Operating margin",
    "net_margin": "Net margin",
}
UNITS: dict[str, Unit] = {
    "shares_outstanding": "shares",
    "gross_margin": "pct",
    "operating_margin": "pct",
    "net_margin": "pct",
}
# Question words -> concepts; checked in order, several can match.
KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"free cash flow|\bfcf\b", ("operating_cash_flow", "capex", "free_cash_flow")),
    (r"net debt", ("debt", "cash", "marketable_securities", "net_debt")),
    (r"gross margin", ("revenue", "cogs", "gross_margin")),
    (r"operating margin|ebit margin", ("revenue", "ebit", "operating_margin")),
    (r"net margin|profit margin", ("revenue", "net_income", "net_margin")),
    # "margin(s)" alone, not part of a specific margin named above.
    (
        r"(?<!gross )(?<!operating )(?<!ebit )(?<!net )(?<!profit )\bmargins?\b",
        ("gross_margin", "operating_margin", "net_margin"),
    ),
    (r"revenue|sales|top line", ("revenue",)),
    (r"cost of (goods|revenue|sales)|\bcogs\b", ("cogs",)),
    (r"\bsg&a\b|selling, general", ("sga",)),
    (r"\br&d\b|research", ("rnd",)),
    (r"\bebit\b(?! margin)|operating (income|profit)", ("ebit",)),
    (r"depreciation|amortization|\bd&a\b", ("depreciation_amortization",)),
    (r"stock-based|share-based|\bsbc\b", ("stock_based_compensation",)),
    (r"interest", ("interest_expense",)),
    (r"income tax|tax expense", ("income_tax_expense",)),
    (r"pre-?tax", ("pretax_income",)),
    (r"net income|net earnings|\bearnings\b|\bprofit\b", ("net_income",)),
    (r"\bcash\b(?! flow)", ("cash",)),
    (r"marketable|short-term investments", ("marketable_securities",)),
    (r"receivable", ("receivables",)),
    (r"inventor", ("inventory",)),
    (r"payable", ("payables",)),
    (r"pp&e|property|plant", ("net_ppe",)),
    (r"goodwill", ("goodwill",)),
    (r"\bdebt\b|borrowing", ("debt",)),
    (r"lease", ("operating_lease_liabilities", "finance_lease_liabilities")),
    (r"\bequity\b|book value", ("equity",)),
    (r"minority|noncontrolling", ("minority_interest",)),
    (r"preferred", ("preferred_equity",)),
    (r"total assets|\bassets\b", ("total_assets",)),
    (r"liabilities", ("total_liabilities",)),
    (r"operating cash flow|cash from operations|\bcfo\b|cash flow", ("operating_cash_flow",)),
    (r"capex|capital expenditure", ("capex",)),
    (r"buyback|repurchase", ("buybacks",)),
    (r"dividend", ("dividends",)),
    (r"shares", ("shares_outstanding",)),
)
DEFAULT: tuple[str, ...] = (
    "revenue",
    "ebit",
    "net_income",
    "operating_cash_flow",
    "capex",
    "cash",
    "debt",
)
# Cost-of-capital items an audit question can ask about.
WACC_ITEMS: tuple[tuple[str, str, str, Unit], ...] = (
    (r"terminal", "terminal_wacc", "Terminal WACC", "pct"),
    (r"\bwacc\b|cost of capital|discount rate", "wacc", "WACC", "pct"),
    (r"risk[- ]free|treasury", "rf", "Risk-free rate", "pct"),
    (r"equity risk premium|\berp\b", "erp", "Equity risk premium", "pct"),
    (r"country risk", "crp", "Country risk premium", "pct"),
    (r"unlevered", "unlevered_beta", "Unlevered industry beta", "ratio"),
    (r"regression", "regression_beta", "Regression beta", "ratio"),
    (r"\bbeta\b", "relevered_beta", "Relevered beta", "ratio"),
    (r"cost of equity", "cost_equity", "Cost of equity", "pct"),
    (r"after-?tax cost of debt", "aftertax_kd", "After-tax cost of debt", "pct"),
    (r"cost of debt|pre-?tax cost of debt", "pretax_kd", "Pre-tax cost of debt", "pct"),
    (r"tax rate", "tax_rate", "Marginal tax rate", "pct"),
    (r"weight", "weight_equity", "Equity weight", "pct"),
)
STATUS_TEXT = {
    "not_presented": "not presented (evidenced zero)",
    "not_disclosed": "not disclosed",
    "unresolved": "unresolved",
}


class SourceEntry(BaseModel):
    """One provenance record; its text may contain identifiers and derivation numbers."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    number: int
    source: str
    ref: str
    period_end: date | None = None
    filed: date | None = None
    note: str | None = None


class Figure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str
    display: str  # the exact text placed in the body, without the marker
    value: Sourced[float]
    source: int


class Answer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    intent: Intent | None
    title: str
    body: str
    figures: tuple[Figure, ...] = ()
    sources: tuple[SourceEntry, ...] = ()

    def text(self) -> str:
        lines = [self.title, "", self.body]
        if self.sources:
            lines += ["", "Sources"]
            for s in self.sources:
                when = f"; filed {s.filed}" if s.filed else ""
                period = f"; period end {s.period_end}" if s.period_end else ""
                lines.append(f"[{s.number}] {s.source}{when}{period}: {s.ref}")
                if s.note:
                    lines.append(f"    {s.note}")
        return "\n".join(lines)


class ConceptChoice(BaseModel):
    concepts: list[Concept] = Field(max_length=8)


def fmt(value: float, unit: Unit) -> str:
    if unit == "pct":
        return f"{value:.2%}"
    if unit == "ratio":
        return f"{value:.2f}"
    sign = "-" if value < 0 else ""
    size = abs(value)
    prefix = "" if unit == "shares" else "$"
    for scale, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if size >= scale:
            return f"{sign}{prefix}{size / scale:,.2f}{suffix}"
    if unit == "usd":  # prices per share and other small amounts keep their cents
        return f"{sign}{prefix}{size:,.2f}"
    return f"{sign}{prefix}{size:,.0f}"


class Citations:
    """Formats figures and numbers their sources; identical evidence shares a number."""

    def __init__(self) -> None:
        self.sources: list[SourceEntry] = []
        self.figures: list[Figure] = []
        self.index: dict[tuple[Any, ...], int] = {}

    def cite(self, label: str, value: Sourced[float], unit: Unit, extra: str = "") -> str:
        prov = value.prov
        note = "; ".join(p for p in (extra, prov.note or "") if p) or None
        key = (prov.source, prov.ref, prov.period_end, prov.filed, note)
        if key not in self.index:
            self.index[key] = len(self.sources) + 1
            self.sources.append(
                SourceEntry(
                    number=self.index[key],
                    source=prov.source,
                    ref=prov.ref,
                    period_end=prov.period_end,
                    filed=prov.filed,
                    note=note,
                )
            )
        display = fmt(value.value, unit)
        self.figures.append(
            Figure(label=label, display=display, value=value, source=self.index[key])
        )
        return f"{display} [{self.index[key]}]"

    def answer(self, intent: Intent | None, title: str, body: str) -> Answer:
        return Answer(
            intent=intent,
            title=title,
            body=body,
            figures=tuple(self.figures),
            sources=tuple(self.sources),
        )


def select_concepts(question: str | None, llm: LocalLLM | None = None) -> tuple[str, ...]:
    """Keyword rules; the model picks from the concept list only when none match."""
    text = (question or "").lower()
    chosen: list[str] = []
    found: list[tuple[int, int, int, str]] = []  # (position, rule, place in rule, concept)
    for order, (pattern, concepts) in enumerate(KEYWORDS):
        match = re.search(pattern, text)
        if match:
            found += [(match.start(), order, i, c) for i, c in enumerate(concepts)]
    for *_, concept in sorted(found):
        if concept not in chosen:
            chosen.append(concept)
    if chosen or not text or llm is None:
        return tuple(chosen) or DEFAULT
    prompt = (
        "Which financial items does this question ask for? Choose only from: "
        + ", ".join(LABELS)
        + f".\nQuestion: {question}"
    )
    try:
        reply = llm.generate("router", prompt, ConceptChoice, prompt_version=PROMPT_VERSION)
    except LLMError:
        return DEFAULT
    return tuple(dict.fromkeys(reply.value.concepts)) or DEFAULT


def derived(
    name: str,
    parts: list[tuple[float, Sourced[float]]],
    as_of: date,
    period_end: date,
    note: str,
    divide_by: Sourced[float] | None = None,
) -> Sourced[float]:
    total = sum(sign * p.value for sign, p in parts)
    if divide_by is not None:
        total /= divide_by.value
    refs = [p.prov.ref for _, p in parts] + ([divide_by.prov.ref] if divide_by else [])
    return Sourced[float](
        value=total,
        prov=Provenance(
            source="derived",
            ref=" | ".join(dict.fromkeys(refs)),
            as_of=as_of,
            period_end=period_end,
            confidence="medium",
            note=f"{LABELS[name]} = {note}",
        ),
    )


def period_value(period: FinancialPeriod, concept: str, as_of: date) -> Sourced[float] | None:
    v = period.values
    end = period.period_end
    if concept in v:
        return v[concept]
    if concept == "free_cash_flow" and {"operating_cash_flow", "capex"} <= v.keys():
        parts = [(1.0, v["operating_cash_flow"]), (-1.0, v["capex"])]
        return derived(concept, parts, as_of, end, "operating cash flow - capex")
    if concept == "net_debt" and {"debt", "cash"} <= v.keys():
        parts = [(1.0, v["debt"]), (-1.0, v["cash"])]
        if "marketable_securities" in v:
            parts.append((-1.0, v["marketable_securities"]))
        return derived(concept, parts, as_of, end, "debt - cash - marketable securities")
    ratios = {
        "gross_margin": ([(1.0, "revenue"), (-1.0, "cogs")], "(revenue - cost of revenue)"),
        "operating_margin": ([(1.0, "ebit")], "EBIT"),
        "net_margin": ([(1.0, "net_income")], "net income"),
    }
    if concept in ratios and "revenue" in v and v["revenue"].value:
        terms, what = ratios[concept]
        if all(name in v for _, name in terms):
            parts = [(sign, v[cast(FinancialConcept, name)]) for sign, name in terms]
            return derived(concept, parts, as_of, end, f"{what} / revenue", v["revenue"])
    return None


def cell(cite: Citations, period: FinancialPeriod, concept: str, as_of: date, label: str) -> str:
    value = period_value(period, concept, as_of)
    status: ConceptStatus | None = period.statuses.get(cast(FinancialConcept, concept))
    if value is None:
        if status is not None and status.status in STATUS_TEXT:
            return STATUS_TEXT[status.status]
        return "not available"
    extra = []
    if status is not None and status.basis:
        extra.append(f"basis: {status.basis}")
    if status is not None and status.status == "carried_forward":
        extra.append(f"carried forward from {status.observed_on}")
    shown = cite.cite(label, value, UNITS.get(concept, "usd"), "; ".join(extra))
    if status is not None and status.status == "carried_forward":
        shown += " (carried forward)"
    return shown


def heading(period: FinancialPeriod) -> str:
    if period.kind == "ttm":
        return f"TTM to {period.period_end}"
    return f"FY{period.fiscal_year} ({period.period_start} to {period.period_end})"


def table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def shares_cell(cite: Citations, bundle: DataBundle) -> str:
    market = bundle.market
    if market is None:
        return "not available"
    return cite.cite(
        "Shares outstanding",
        market.shares_outstanding,
        "shares",
        f"cover-page count measured {market.shares_observed_on}",
    )


def data_answer(bundle: DataBundle, request: ValuationRequest, concepts: tuple[str, ...]) -> Answer:
    cite = Citations()
    raw = bundle.raw
    as_of = request.valuation_date
    title = f"{request.company_name} ({request.ticker}): reported figures as of {as_of}"
    if raw is None or not raw.annual:
        return cite.answer("I1", title, "No reported financial history was collected.")
    periods = [raw.annual[-1]] + ([raw.ttm] if raw.ttm is not None else [])
    header = ["Item"] + [
        (f"At {p.period_end}" if all(c in STOCKS for c in concepts) else heading(p))
        for p in periods
    ]
    rows = []
    for concept in concepts:
        label = LABELS[concept]
        if concept == "shares_outstanding":
            rows.append([label, shares_cell(cite, bundle)] + [""] * (len(periods) - 1))
            continue
        cells = [cell(cite, p, concept, as_of, f"{label}, {heading(p)}") for p in periods]
        rows.append([label + (" (end of period)" if concept in STOCKS else ""), *cells])
    body = table(header, rows)
    return cite.answer("I1", title, body)


def history_answer(
    bundle: DataBundle, request: ValuationRequest, concepts: tuple[str, ...]
) -> Answer:
    cite = Citations()
    raw = bundle.raw
    title = f"{request.company_name} ({request.ticker}): history as of {request.valuation_date}"
    if raw is None or not raw.annual:
        return cite.answer("I2", title, "No reported financial history was collected.")
    periods = list(raw.annual) + ([raw.ttm] if raw.ttm is not None else [])
    header = ["Item"] + [
        f"TTM to {p.period_end}" if p.kind == "ttm" else f"FY{p.fiscal_year}" for p in periods
    ]
    rows = [
        [LABELS[c]]
        + [cell(cite, p, c, request.valuation_date, f"{LABELS[c]}, {heading(p)}") for p in periods]
        for c in concepts
        if c != "shares_outstanding"
    ]
    body = table(header, rows)
    body += "\n\nFiscal years: " + "; ".join(
        f"FY{p.fiscal_year} {p.period_start} to {p.period_end}" for p in raw.annual
    )
    return cite.answer("I2", title, body)


def band_values(wacc: WACCResult) -> tuple[Sourced[float], Sourced[float]]:
    prov = wacc.wacc.prov.model_copy(
        update={"note": "WACC band; " + "; ".join(n for n in wacc.method_notes if "Band" in n)}
    )
    low, high = wacc.wacc_band
    return Sourced[float](value=low, prov=prov), Sourced[float](value=high, prov=prov)


def wacc_answer(
    request: ValuationRequest,
    bundle: DataBundle | None,
    wacc: WACCResult | None,
    reason: str,
    decision: Any = None,
) -> Answer:
    cite = Citations()
    title = (
        f"{request.company_name} ({request.ticker}): cost of capital as of {request.valuation_date}"
    )
    if wacc is None:
        return cite.answer("I3", title, "WACC was not computed. " + (reason or ""))
    notes = "; ".join(wacc.method_notes)
    low, high = band_values(wacc)
    industry = bundle.industry.name if bundle is not None and bundle.industry else "industry"
    beta_row = {
        "bottom_up": "bottom-up (industry beta relevered)",
        "regression": "regression",
        "average": "average of bottom-up and regression",
    }[wacc.beta_method]
    rows: list[list[str]] = [
        ["Risk-free rate (ten-year Treasury)", cite.cite("Risk-free rate", wacc.rf, "pct")],
        [
            "Equity risk premium (mature market)",
            cite.cite("Equity risk premium", wacc.erp, "pct"),
        ],
        ["Country risk premium", cite.cite("Country risk premium", wacc.crp, "pct")],
        [
            f"Unlevered beta ({industry})",
            cite.cite("Unlevered beta", wacc.unlevered_beta, "ratio"),
        ],
        [
            "Relevered beta (at market debt to equity)",
            cite.cite("Relevered beta", wacc.relevered_beta, "ratio"),
        ],
        [
            "Regression beta",
            cite.cite("Regression beta", wacc.regression_beta, "ratio")
            if wacc.regression_beta is not None
            else "not available",
        ],
        ["Beta used", beta_row],
        ["Beta method chosen by", chooser(decision)],
        ["Cost of equity", cite.cite("Cost of equity", wacc.cost_equity, "pct")],
        ["Pre-tax cost of debt", cite.cite("Pre-tax cost of debt", wacc.pretax_kd, "pct")],
        ["Marginal tax rate", cite.cite("Marginal tax rate", wacc.tax_rate, "pct")],
        ["After-tax cost of debt", cite.cite("After-tax cost of debt", wacc.aftertax_kd, "pct")],
        ["Equity weight", cite.cite("Equity weight", wacc.weight_equity, "pct")],
        ["Debt weight", cite.cite("Debt weight", wacc.weight_debt, "pct")],
        ["WACC", cite.cite("WACC", wacc.wacc, "pct", "method notes: " + notes)],
        ["Terminal WACC", cite.cite("Terminal WACC", wacc.terminal_wacc, "pct")],
    ]
    body = (
        f"WACC {cite.cite('WACC', wacc.wacc, 'pct', 'method notes: ' + notes)}, "
        f"range {cite.cite('WACC band low', low, 'pct')} to "
        f"{cite.cite('WACC band high', high, 'pct')}.\n\n" + table(["Component", "Value"], rows)
    )
    return cite.answer("I3", title, body)


def chooser(decision: Any) -> str:
    """Who chose the beta method, in words (the reasons are in the WACC source entry)."""
    if decision is None:
        return "default rule"
    if decision.source == "rule":
        text = "default rule"
        if decision.rejected:
            text += " (a model choice lacked supporting evidence and was rejected)"
        return text
    if decision.flagged:
        return f"WACC Agent, departing from the default rule ({decision.rule_method}): review"
    return "WACC Agent, agreeing with the default rule"


def audit_answer(
    request: ValuationRequest,
    bundle: DataBundle | None,
    wacc: WACCResult | None,
    question: str,
    concepts: tuple[str, ...],
) -> Answer:
    """The provenance chain of the values a question names."""
    cite = Citations()
    title = f"{request.company_name} ({request.ticker}): where the numbers come from"
    lines: list[str] = []
    text = question.lower()
    for pattern, field, label, unit in WACC_ITEMS:
        if wacc is not None and re.search(pattern, text):
            value = getattr(wacc, field)
            if value is not None:
                lines.append(
                    f"{label}: {cite.cite(label, value, unit)}; "
                    f"{describe(value.prov)}; full chain in the source entry."
                )
            break
    if not lines and bundle is not None and bundle.raw is not None and bundle.raw.annual:
        raw = bundle.raw
        periods = [raw.annual[-1]] + ([raw.ttm] if raw.ttm is not None else [])
        for concept in concepts:
            if concept == "shares_outstanding":
                lines.append(f"Shares outstanding: {shares_cell(cite, bundle)}.")
                continue
            for period in periods:
                label = f"{LABELS[concept]}, {heading(period)}"
                shown = cell(cite, period, concept, request.valuation_date, label)
                value = period_value(period, concept, request.valuation_date)
                how = describe(value.prov) if value is not None else "no value"
                lines.append(f"{label}: {shown}; {how}.")
    if not lines:
        lines.append(
            "No value in this session matches the question; name an item such as revenue, "
            "debt, cash, beta or WACC."
        )
    return cite.answer("I10", title, "\n".join(lines))


def describe(prov: Provenance) -> str:
    if prov.source == "sec_xbrl":
        return f"reported in an SEC filing filed {prov.filed}, tagged in its XBRL"
    if prov.source == "derived":
        note = prov.note or ""
        if note.startswith("TTM sum"):
            return "sum of the four latest quarters, each from its filing"
        if note.startswith("Quarter = cumulative"):
            return "year-to-date figure less the previous year-to-date figure"
        if re.match(r"(balance_sheet|income_statement|cash_flow) ", note) and " | +" in note:
            return "sum of statement lines, each tagged in the filing"
        if "carried" in note.lower():
            return "carried forward from an earlier dated filing"
        return "calculated from the cited inputs (steps in the source entry)"
    return {
        "fred": "FRED (Federal Reserve Bank of St. Louis) series",
        "damodaran": "Damodaran (NYU Stern) dataset",
        "yfinance": "Yahoo Finance price history",
        "stooq": "Stooq price history",
        "user_override": "a policy or user setting",
        "sec_filing_text": "the text of an SEC filing",
        "llm_estimate": "a model estimate",
    }[prov.source]


# Questions about what the filings' notes add, by topic.
FILING_TOPICS: tuple[tuple[str, str], ...] = (
    (
        "dilution",
        r"dilut|stock options?|\boptions\b|\brsus?\b|restricted stock|share units|"
        r"performance shares|psus?\b|exercise price",
    ),
    ("leases", r"lease (discount )?rate|discount rate .*lease|lease liabilit"),
    ("tax", r"marginal tax|tax rate"),
    ("captive", r"captive|financial services|finance (arm|subsidiary)|industrial debt"),
)
NORMALIZED = re.compile(r"clean|normali[sz]|adjust|one-?off|non-?recurring|restructur|impair")
QUOTE = 300


def topics(question: str) -> list[str]:
    text = question.lower()
    return [name for name, pattern in FILING_TOPICS if re.search(pattern, text)]


def filings_answer(
    request: ValuationRequest, bundle: DataBundle, wanted: list[str], intent: Intent | None
) -> Answer:
    """What the filings' notes say on dilution, leases, the tax rate and captive finance."""
    cite = Citations()
    title = f"{request.company_name} ({request.ticker}): from the filings' notes"
    insights = bundle.filing_insights
    if insights is None:
        return cite.answer(intent, title, "The filings' notes were not read in this session.")
    parts: list[str] = []
    if "dilution" in wanted:
        rows = [
            [
                s.security_id,
                s.kind,
                cite.cite(f"{s.security_id} count", s.count, "shares"),
                cite.cite(f"{s.security_id} exercise price", s.strike_price, "usd")
                if s.strike_price is not None
                else "",
            ]
            for s in insights.dilutive_securities
        ]
        parts.append(
            "Dilutive securities (options by the treasury stock method; unvested units in "
            "full):\n\n" + table(["Security", "Kind", "Count", "Exercise price"], rows)
            if rows
            else "No options or unvested share units were read from the filings."
        )
    if "leases" in wanted:
        lines = [
            f"{x.lease_type.capitalize()} leases: liability "
            f"{cite.cite('Lease liability', x.liability, 'usd')}"
            + (
                f", weighted-average discount rate "
                f"{cite.cite('Lease discount rate', x.discount_rate, 'pct')}"
                if x.discount_rate is not None
                else ""
            )
            for x in insights.lease_liabilities
        ]
        parts.append("\n".join(lines) or "No lease discount rate was read from the filings.")
    if "tax" in wanted:
        lines = [
            f"Marginal tax rate {cite.cite('Marginal tax rate', x.amount, 'pct')}: {x.description}"
            for x in insights.tax_items
            if x.amount is not None
        ]
        parts.append(
            "\n".join(lines) or "The rate reconciliation was not read; the 25% policy rate applies."
        )
    if "captive" in wanted:
        lines = [
            f"{x.item_id.split(':', 1)[-1]}: "
            f"{cite.cite('Captive-finance debt', x.amount, 'usd')}; {x.description}"
            for x in insights.captive_finance
            if x.amount is not None
        ]
        parts.append(
            "\n".join(lines) or "No captive-finance split was needed or read from the filings."
        )
    return cite.answer(intent, title, "\n\n".join(parts))


def normalized_section(cite: Citations, state: Any) -> str:
    """Clean EBIT by year and the approved adjustments, each with its filing passage."""
    normalized = state.get("normalized")
    approved: tuple[Adjustment, ...] = state.get("approved_adjustments", ())
    if normalized is None:
        return ""
    passages = {
        p.adjustment.adjustment_id: p.passage for p in state.get("proposals", ()) if p.passage
    }
    periods = [*normalized.annual, *((normalized.ttm,) if normalized.ttm else ())]
    rows = [
        [
            f"TTM to {p.period_end}" if p.kind == "ttm" else str(p.period_end),
            cite.cite(f"Clean EBIT {p.period_end}", p.clean_ebit, "usd"),
        ]
        for p in periods
    ]
    lines = [
        "Normalized EBIT (approved adjustments applied):",
        "",
        table(["Period", "Clean EBIT"], rows),
    ]
    if approved:
        lines += ["", "Approved adjustments:"]
        for a in approved:
            sign = "+" if a.direction == "increase" else "-"
            lines.append(
                f"- {a.period_end} {a.item}: {sign}"
                f"{cite.cite(a.item, a.amount, 'usd', a.rationale[:200])}"
            )
            quote = passages.get(a.adjustment_id)
            if quote:
                lines.append(f'  > "{" ".join(quote.split())[:QUOTE]}..."')
                where = next((c for c in a.evidence if c.table_id), a.evidence[0])
                lines.append(f"  (filing {where.accession}, {where.section})")
    else:
        lines += ["", "No adjustments were approved: clean EBIT is reported EBIT (with leases)."]
    return "\n".join(lines)


# The assumption a "why" question names (I4), by its rationale key.
ASSUMPTIONS: tuple[tuple[str, str], ...] = (
    ("terminal_growth", r"terminal|long[- ]run growth|perpetual"),
    ("terminal_roic", r"roic|moat|return on (invested )?capital"),
    ("reinvestment", r"reinvest|sales[- ]to[- ]capital|capital (need|intensity)|capex"),
    ("tax", r"tax"),
    ("margin", r"margin|profitab"),
    ("growth", r"growth|revenue|sales"),
)
YEARS_SHOWN = (0, 4, 9)  # forecast years 1, 5 and 10


def engine_value(value: float, ref: str, as_of: date, note: str) -> Sourced[float]:
    return Sourced[float](
        value=value,
        prov=Provenance(source="derived", ref=ref, as_of=as_of, confidence="medium", note=note),
    )


def scenario_of(question: str) -> str:
    text = question.lower()
    return next((s for s in ("bear", "bull") if re.search(rf"\b{s}", text)), "base")


def unavailable(request: ValuationRequest, state: Any, what: str) -> Answer:
    notes = list(state.get("forecast_notes", ())) or [state.get("wacc_error", "")]
    reason = "; ".join(n for n in notes if n)[:400] or "the forecast or the valuation was not built"
    return Citations().answer(
        request.intent,
        f"{request.company_name} ({request.ticker}): {what}",
        f"Not available in this session: {reason}.",
    )


def value_answer(request: ValuationRequest, state: Any) -> Answer:
    """Value per share (I6): the weighted value, each scenario, and the market for context."""
    valuation = state.get("valuation")
    scenarios: ForecastScenarios | None = state.get("forecast")
    if valuation is None or scenarios is None:
        return unavailable(request, state, "value per share")
    cite = Citations()
    as_of = request.valuation_date
    by = {f.scenario: f for f in scenarios.scenarios}
    rows = []
    for result in valuation.results:
        f = by[result.scenario]
        ref = f"engine {result.calculation_ref} (input {result.input_hash[:12]})"
        rows.append(
            [
                result.scenario,
                f"{valuation.probabilities[result.scenario]:.0%}",
                cite.cite(
                    f"{result.scenario} value per share",
                    engine_value(result.value_per_share, ref, as_of, "DCF value per share"),
                    "usd",
                ),
                cite.cite(f"{result.scenario} growth, year 1", f.revenue_growth[0], "pct"),
                cite.cite(f"{result.scenario} margin, year 5", f.operating_margin[4], "pct"),
                cite.cite(f"{result.scenario} terminal growth", f.terminal_growth, "pct"),
            ]
        )
    weighted = engine_value(
        valuation.weighted, "engine: probability-weighted scenarios", as_of, "weighted value"
    )
    lines = [
        f"Value per share (probability-weighted): {cite.cite('Weighted value', weighted, 'usd')}, "
        f"range {valuation.low:,.2f} to {valuation.high:,.2f} across the scenarios.",
        "",
        table(
            [
                "Scenario",
                "Probability",
                "Value per share",
                "Growth, year 1",
                "Margin, year 5",
                "Terminal growth",
            ],
            rows,
        ),
    ]
    bundle: DataBundle | None = state.get("data")
    if bundle is not None and bundle.market is not None:
        price = bundle.market.close.price
        lines += [
            "",
            f"Market price {cite.cite('Market price', price, 'usd')} on "
            f"{bundle.market.close.observed_on}, shown for context only: it is not an input.",
        ]
    review = [n for n in state.get("forecast_notes", ()) if n.startswith(("Gate 2", "base value"))]
    if review:
        lines += ["", "For review:"] + [f"- {n}" for n in review]
    if valuation.workbook:
        lines += ["", f"Workbook: {valuation.workbook}"]
    return cite.answer(
        request.intent,
        f"{request.company_name} ({request.ticker}): value as of {as_of}",
        "\n".join(lines),
    )


def scenario_answer(request: ValuationRequest, state: Any) -> Answer:
    """The scenarios (I5): drivers in years 1, 5 and 10, value, and each story."""
    scenarios: ForecastScenarios | None = state.get("forecast")
    if scenarios is None:
        return unavailable(request, state, "scenarios")
    cite = Citations()
    valuation = state.get("valuation")
    parts = []
    for f in scenarios.scenarios:
        rows = [
            [
                f"Year {k + 1} ({f.year_ends[k].year})",
                cite.cite(f"{f.scenario} growth, year {k + 1}", f.revenue_growth[k], "pct"),
                cite.cite(f"{f.scenario} margin, year {k + 1}", f.operating_margin[k], "pct"),
                cite.cite(
                    f"{f.scenario} sales to capital, year {k + 1}", f.sales_to_capital[k], "ratio"
                ),
            ]
            for k in YEARS_SHOWN
            if k < len(f.year_ends)
        ]
        result = valuation.scenario(f.scenario) if valuation is not None else None
        head = f"{f.scenario.capitalize()} ({f.probability:.0%})" + (
            f": {result.value_per_share:,.2f} per share" if result is not None else ""
        )
        parts.append(
            f"{head}\n{f.narrative}\n\n"
            + table(["Year", "Revenue growth", "Operating margin", "Sales to capital"], rows)
        )
    return cite.answer(
        request.intent, f"{request.company_name} ({request.ticker}): scenarios", "\n\n".join(parts)
    )


def sensitivity_answer(request: ValuationRequest, state: Any) -> Answer:
    """Sensitivity (I7): base value per share by WACC and terminal growth."""
    valuation = state.get("valuation")
    if valuation is None or not valuation.grid:
        return unavailable(request, state, "sensitivity")
    header = ["WACC \\ terminal growth"] + [f"{g:.2%}" for g in valuation.growths]
    rows = [
        [f"{w:.2%}"] + ["-" if v is None else f"{v:,.2f}" for v in row]
        for w, row in zip(valuation.waccs, valuation.grid, strict=True)
    ]
    base = valuation.scenario("base")
    body = (
        "Base-scenario value per share as WACC and terminal growth change (engine reruns; "
        "'-' where terminal growth is not below WACC):\n\n"
        + table(header, rows)
        + (f"\n\nBase value: {base.value_per_share:,.2f}." if base is not None else "")
    )
    return Citations().answer(
        request.intent, f"{request.company_name} ({request.ticker}): sensitivity", body
    )


def why_answer(request: ValuationRequest, state: Any) -> Answer:
    """Why an assumption (I4): its rule, the evidence, and its values."""
    scenarios: ForecastScenarios | None = state.get("forecast")
    if scenarios is None:
        return unavailable(request, state, "assumption rationale")
    question = (request.question or "").lower()
    name = scenario_of(question)
    item: ForecastAssumptions = next(f for f in scenarios.scenarios if f.scenario == name)
    key = next((k for k, pattern in ASSUMPTIONS if re.search(pattern, question)), None)
    keys = [key] if key else [k for k, _ in ASSUMPTIONS if k in item.rationale]
    cite = Citations()
    lines = []
    for k in keys:
        if k not in item.rationale:
            continue
        values = {
            "growth": (item.revenue_growth[0], "growth, year 1"),
            "margin": (item.operating_margin[4], "margin, year 5"),
            "reinvestment": (item.sales_to_capital[0], "sales to capital"),
            "tax": (item.tax_rate[0], "tax rate, year 1"),
            "terminal_growth": (item.terminal_growth, "terminal growth"),
            "terminal_roic": (item.terminal_roic, "terminal ROIC"),
        }
        value, label = values.get(k, (None, ""))
        unit: Unit = "ratio" if k == "reinvestment" else "pct"
        shown = f" ({label} {cite.cite(f'{name} {label}', value, unit)})" if value else ""
        lines.append(f"{k.replace('_', ' ').capitalize()}{shown}: {item.rationale[k]}")
    review = [n for n in state.get("forecast_notes", ()) if n.startswith("Gate 2")]
    if review:
        lines += ["", "Model choices the statistics did not support (the rule applied):"]
        lines += [f"- {n.removeprefix('Gate 2: ')}" for n in review]
    return cite.answer(
        request.intent,
        f"{request.company_name} ({request.ticker}): why, {name} scenario",
        "\n".join(lines) or "No rationale was recorded for that assumption.",
    )


def summary_answer(request: ValuationRequest, bundle: DataBundle | None, plan_gaps: str) -> Answer:
    cite = Citations()
    title = f"{request.company_name} ({request.ticker}) as of {request.valuation_date}"
    lines = []
    if bundle is not None:
        blocked = [k for k, r in bundle.readiness.items() if r.status == "blocked"]
        lines.append(
            "Data collected; "
            + (
                f"readiness blocked for {', '.join(blocked)}."
                if blocked
                else "no readiness blocks."
            )
        )
    if plan_gaps:
        lines.append(plan_gaps)
    return cite.answer(request.intent, title, "\n".join(lines) or "Nothing to report yet.")


def render(state: Any, llm: LocalLLM | None = None) -> Answer:
    """The answer for the request's intent, from the graph state."""
    request: ValuationRequest = state["request"]
    bundle: DataBundle | None = state.get("data")
    wacc: WACCResult | None = state.get("wacc")
    question = request.question or ""
    intent = request.intent
    if intent in ("I1", "I2", "I10"):
        if bundle is None:
            return summary_answer(request, None, "No data was collected.")
        wanted = topics(question)
        if wanted and intent in ("I1", "I10"):
            return filings_answer(request, bundle, wanted, intent)
        if intent == "I10" and NORMALIZED.search(question.lower()) and state.get("normalized"):
            cite = Citations()
            title = f"{request.company_name} ({request.ticker}): where clean EBIT comes from"
            return cite.answer(intent, title, normalized_section(cite, state))
        concepts = select_concepts(question, llm)
        if intent == "I1":
            return data_answer(bundle, request, concepts)
        if intent == "I2":
            answer = history_answer(bundle, request, concepts)
            if state.get("normalized") is None:
                return answer
            # The same numbering continues in the normalized section.
            cite = Citations()
            cite.sources, cite.figures = list(answer.sources), list(answer.figures)
            cite.index = {
                (s.source, s.ref, s.period_end, s.filed, s.note): s.number for s in answer.sources
            }
            section = normalized_section(cite, state)
            return cite.answer("I2", answer.title, answer.body + "\n\n" + section)
        return audit_answer(request, bundle, wacc, question, concepts)
    if intent == "I3":
        return wacc_answer(
            request, bundle, wacc, state.get("wacc_error", ""), state.get("wacc_decision")
        )
    if intent in ("I6", "I9"):
        return value_answer(request, state)
    if intent == "I5":
        return scenario_answer(request, state)
    if intent == "I7":
        return sensitivity_answer(request, state)
    if intent == "I4":
        return why_answer(request, state)
    done = ("data", "normalize", "wacc", "forecast", "value", "excel", "answer")
    gaps = [s for s in request.plan if s not in done]
    note = ("Not available yet in this phase: " + ", ".join(gaps) + ".") if gaps else ""
    return summary_answer(request, bundle, note)


Renderer = Callable[[Any], Answer]
CONCEPTS: tuple[str, ...] = tuple(get_args(FinancialConcept)) + tuple(get_args(Derived))
