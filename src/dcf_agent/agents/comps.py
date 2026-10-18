"""Comps: relative valuation as a cross-check of the DCF, every number from filings and prices.

1. Candidates (code): companies in the target's Damodaran industry (the classification
   that assigned the target), with an SEC CIK, screened by size: annual revenue within
   0.2x to 5x the target's, from SEC frames (one request for all filers; used only to
   rank sizes, never as a value), at most ten, closest in size first.
2. Keep or drop (model, one call): a verdict and a one-line reason per candidate, from
   its name, SIC description and size; on failure every candidate is kept.
3. Figures (code): each peer's and the target's own TTM revenue, operating income,
   EBITDA (operating income plus depreciation and amortization) and net income, latest
   debt, cash and cover-page shares, all from their SEC company facts filed on or before
   the valuation date; the price on the valuation date. The same definitions for the
   target and every peer (reported figures, no adjustments).
4. Multiples: EV/EBITDA, EV/Sales, EV/EBIT, P/E (EV = market value of equity + debt -
   cash); a negative or missing denominator gives no multiple. Outliers by the
   interquartile rule; quartiles; the target's implied value per share at each median.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median, quantiles
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from dcf_agent.contracts.modeling import CompsResult, MultipleSummary, PeerMultiples
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.cache import DataUnavailable
from dcf_agent.data.prices import PriceClient
from dcf_agent.data.sec import Fact, SECClient, SECError, normalize_cik
from dcf_agent.llm import LLMError, LocalLLM

PROMPT_VERSION = "comps-v2"
STALE = timedelta(days=550)  # about 18 months: older figures are not the company's latest
RESTORE_FIT = 2  # a dropped candidate this close or closer may fill the sample to five
# Peers' revenue against the target's: the band widens when an industry is concentrated.
BANDS = ((0.2, 5.0), (0.05, 20.0), (0.0, math.inf))
CANDIDATES = 10
IQR = 1.5
MIN_PEERS = 5
Metric = Literal["ev_ebitda", "ev_sales", "ev_ebit", "price_earnings"]
METRICS: tuple[Metric, ...] = ("ev_ebitda", "ev_sales", "ev_ebit", "price_earnings")
REVENUE = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
EBIT = ("OperatingIncomeLoss",)
DEPRECIATION = (
    "DepreciationDepletionAndAmortization",
    "DepreciationAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "Depreciation",
)
NET_INCOME = ("NetIncomeLoss", "ProfitLoss")
DEBT_TOTAL = ("LongTermDebt", "DebtLongtermAndShorttermCombinedAmount")
DEBT_PARTS = (
    ("LongTermDebtNoncurrent", "LongTermDebtCurrent"),
    ("ShortTermBorrowings", "CommercialPaper"),
)
CASH = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
)
SECURITIES = (
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
)


@dataclass(frozen=True)
class Candidate:
    ticker: str
    cik: str
    name: str
    sic_description: str | None
    revenue: float


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    keep: bool
    fit: int = Field(default=3, ge=1, le=5)  # 5: same business; 1: unrelated
    reason: str = Field(max_length=300)


class Verdicts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[Verdict] = Field(max_length=12)


@dataclass
class Figures:
    """One company's figures for multiples, each sourced."""

    ticker: str
    cik: str
    values: dict[str, Sourced[float]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Names filled from the valuation's own data (not an independent read of the filings).
    filled: set[str] = field(default_factory=set)

    def fill(self, own: dict[str, Sourced[float]]) -> None:
        """Missing figures from the valuation's own sourced data; EBITDA rebuilt if needed."""
        for name, item in own.items():
            if name not in self.values:
                self.values[name] = item
                self.filled.add(name)
        if "ebitda" not in self.values and "ebit" in self.values and "depreciation" in self.values:
            ebit, dep = self.values["ebit"], self.values["depreciation"]
            self.values["ebitda"] = ebit.model_copy(
                update={
                    "value": ebit.value + dep.value,
                    "prov": ebit.prov.model_copy(
                        update={"note": f"operating income + D&A ({dep.prov.note or ''})"}
                    ),
                }
            )
            self.filled.add("ebitda")
        if self.filled:
            self.notes.append("filled from the valuation's data: " + ", ".join(sorted(self.filled)))

    def get(self, name: str) -> float | None:
        item = self.values.get(name)
        return item.value if item is not None else None


def facts_url(cik: str) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{normalize_cik(cik)}.json"


def frame_revenues(sec: SECClient, year: int) -> dict[str, float]:
    """Calendar-year revenue for every filer (SEC frames), for size screening only."""
    found: dict[str, float] = {}
    for tag in REVENUE:
        try:
            body = sec._json(
                f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY{year}.json"
            )
        except SECError:
            continue
        for row in body.get("data", []):
            cik = normalize_cik(str(row.get("cik", "")))
            value = float(row.get("val") or 0)
            found[cik] = max(found.get(cik, 0.0), value)
    return found


def candidates(
    classification: Sequence[dict[str, str]],
    industry: str,
    target: str,
    ticker_map: dict[str, str],
    revenues: dict[str, float],
    target_revenue: float,
    profile: Callable[[str], dict[str, str | None]],
) -> list[Candidate]:
    """Same industry, an SEC CIK, revenue within the band; closest in size first."""
    found = []
    for row in classification:
        ticker = row["ticker"].upper().replace(".", "-")
        if row.get("industry") != industry or ticker == target.upper():
            continue
        cik = ticker_map.get(ticker) or ticker_map.get(ticker.replace("-", "."))
        if cik is None:
            continue
        revenue = revenues.get(normalize_cik(cik))
        if not revenue or target_revenue <= 0:
            continue
        ratio = revenue / target_revenue
        found.append(
            (abs(math.log(ratio)), ticker, normalize_cik(cik), row.get("company", ""), revenue)
        )
    found.sort()
    # The size band widens until there are enough candidates to allow some drops.
    for low, high in BANDS:
        inside = (
            [f for f in found if low <= f[4] / target_revenue <= high] if target_revenue else []
        )
        if len(inside) >= MIN_PEERS + 2:
            break
    out = []
    for _, ticker, cik, name, revenue in inside[:CANDIDATES]:
        info = profile(cik)
        out.append(
            Candidate(ticker, cik, info.get("name") or name, info.get("sic_description"), revenue)
        )
    return out


def by_tag(facts: Sequence[Fact], tags: Sequence[str]) -> list[Fact]:
    for tag in tags:
        own = [f for f in facts if f.namespace == "us-gaap" and f.tag == tag and f.unit == "USD"]
        if own:
            return own
    return []


def ttm_one(facts: Sequence[Fact], tag: str, as_of: date) -> tuple[float, Fact, str] | None:
    """One tag's latest fiscal year, plus the year to date, less the prior year to date."""
    own = [f for f in by_tag(facts, (tag,)) if f.start is not None and f.end <= as_of]
    years = [f for f in own if timedelta(days=340) < f.end - f.start < timedelta(days=390)]  # type: ignore[operator]
    if not years:
        return None
    year = max(years, key=lambda f: (f.end, f.filed))
    current = [
        f
        for f in own
        if f.start is not None and f.start > year.end and f.end - f.start < timedelta(days=340)
    ]
    if not current:
        return year.value, year, f"fiscal year to {year.end}"
    now = max(current, key=lambda f: (f.end, f.filed))
    length = now.end - (now.start or now.end)
    prior = [
        f
        for f in own
        if f.start is not None
        and abs((f.end - (now.end - timedelta(days=364))).days) <= 10
        and abs(((f.end - f.start) - length).days) <= 10
    ]
    if not prior:
        return year.value, year, f"fiscal year to {year.end} (no prior year to date)"
    before = max(prior, key=lambda f: f.filed)
    return year.value + now.value - before.value, now, f"TTM to {now.end}: FY + YTD - prior YTD"


def fresh(end: date, as_of: date) -> bool:
    """A figure no older than STALE: a tag a company stopped using is not its latest."""
    return as_of - end <= STALE


def ttm(facts: Sequence[Fact], tags: Sequence[str], as_of: date) -> tuple[float, Fact, str] | None:
    """The TTM figure of the tag with the latest period (tags are alternatives over time)."""
    found = [r for r in (ttm_one(facts, t, as_of) for t in tags) if r is not None]
    found = [r for r in found if fresh(r[1].end, as_of)]
    return max(found, key=lambda r: r[1].end, default=None)  # ties: the first tag


def latest(facts: Sequence[Fact], tags: Sequence[str], as_of: date) -> Fact | None:
    """The latest balance-sheet value across the tags, if recent."""
    own = [
        f
        for t in tags
        for f in by_tag(facts, (t,))
        if f.start is None and f.end <= as_of and fresh(f.end, as_of)
    ]
    return max(own, key=lambda f: (f.end, f.filed), default=None)


def sourced(value: float, fact: Fact, cik: str, as_of: date, note: str) -> Sourced[float]:
    return Sourced[float](
        value=value,
        prov=Provenance(
            source="sec_xbrl",
            ref=f"{facts_url(cik)} (accession {fact.accession})",
            as_of=as_of,
            period_end=fact.end,
            filed=fact.filed,
            confidence="high",
            note=note,
        ),
    )


def figures(sec: SECClient, prices: PriceClient, ticker: str, cik: str, as_of: date) -> Figures:
    """A company's TTM figures, balance sheet, shares and price, point in time."""
    out = Figures(ticker, normalize_cik(cik))
    try:
        facts = sec.get_company_facts(cik, as_of)
    except SECError as error:
        out.notes.append(f"company facts unavailable: {error}")
        return out
    for name, tags in (
        ("revenue", REVENUE),
        ("ebit", EBIT),
        ("depreciation", DEPRECIATION),
        ("net_income", NET_INCOME),
    ):
        found = ttm(facts, tags, as_of)
        if found is not None:
            value, fact, how = found
            out.values[name] = sourced(value, fact, cik, as_of, f"{fact.tag}: {how}")
    if "ebit" in out.values and "depreciation" in out.values:
        ebit, dep = out.values["ebit"], out.values["depreciation"]
        out.values["ebitda"] = ebit.model_copy(
            update={
                "value": ebit.value + dep.value,
                "prov": ebit.prov.model_copy(
                    update={"note": f"operating income + D&A ({dep.prov.note})"}
                ),
            }
        )
    debt = latest(facts, DEBT_TOTAL, as_of)
    if debt is not None:
        extra = [latest(facts, (t,), as_of) for t in DEBT_PARTS[1]]
        total = debt.value + sum(f.value for f in extra if f is not None and f.end == debt.end)
        out.values["debt"] = sourced(total, debt, cik, as_of, f"{debt.tag} + short-term borrowings")
    else:
        parts = [latest(facts, (t,), as_of) for group in DEBT_PARTS for t in group]
        present = [f for f in parts if f is not None]
        if present:
            end = max(f.end for f in present)
            same = [f for f in present if f.end == end]
            out.values["debt"] = sourced(
                sum(f.value for f in same), same[0], cik, as_of, "+".join(f.tag for f in same)
            )
        else:
            out.notes.append("no debt tagged: taken as zero")
    cash = latest(facts, CASH, as_of)
    if cash is not None:
        securities = latest(facts, SECURITIES, as_of)
        total = cash.value + (
            securities.value if securities is not None and securities.end == cash.end else 0.0
        )
        out.values["cash"] = sourced(
            total, cash, cik, as_of, f"{cash.tag} + short-term investments"
        )
        # Cash alone, for the Critic's like-for-like check of the bridge.
        out.values["cash_only"] = sourced(cash.value, cash, cik, as_of, cash.tag)
    shares = [
        f
        for f in facts
        if f.namespace == "dei" and f.tag == "EntityCommonStockSharesOutstanding" and f.end <= as_of
    ]
    if shares:
        cover = max(shares, key=lambda f: (f.end, f.filed))
        same = [f for f in shares if f.end == cover.end and f.accession == cover.accession]
        out.values["shares"] = Sourced[float](
            value=sum(f.value for f in same),  # every class on the cover
            prov=Provenance(
                source="sec_xbrl",
                ref=f"{facts_url(cik)} (accession {cover.accession})",
                as_of=as_of,
                period_end=cover.end,
                filed=cover.filed,
                confidence="high",
                note="cover-page shares outstanding (all classes)",
            ),
        )
    try:
        history = prices.get_price_history(ticker, as_of - timedelta(days=10), as_of)
    except (DataUnavailable, ValueError) as error:
        out.notes.append(f"price unavailable: {error}")
        return out
    if history.observations:
        out.values["price"] = history.observations[-1].price
    return out


def multiples(f: Figures) -> dict[Metric, Sourced[float]]:
    """EV multiples and P/E; none when an input is missing or a denominator is not positive."""
    price, shares = f.get("price"), f.get("shares")
    if price is None or shares is None or shares <= 0:
        return {}
    equity = price * shares
    ev = equity + (f.get("debt") or 0.0) - (f.get("cash") or 0.0)
    refs = " | ".join(
        dict.fromkeys(
            v.prov.ref for k, v in f.values.items() if k in ("price", "shares", "debt", "cash")
        )
    )
    out: dict[Metric, Sourced[float]] = {}
    for metric, denominator, top, label in (
        ("ev_ebitda", "ebitda", ev, "EV / EBITDA"),
        ("ev_sales", "revenue", ev, "EV / revenue"),
        ("ev_ebit", "ebit", ev, "EV / operating income"),
        ("price_earnings", "net_income", equity, "market value / net income"),
    ):
        base = f.values.get(denominator)
        if base is None or base.value <= 0:
            continue
        out[metric] = Sourced[float](  # type: ignore[index]
            value=top / base.value,
            prov=Provenance(
                source="derived",
                ref=f"{refs} | {base.prov.ref}",
                as_of=base.prov.as_of,
                confidence="medium",
                note=f"{label}: EV = price x shares + debt - cash = {ev:,.0f}; "
                f"{denominator} {base.value:,.0f} ({base.prov.note})",
            ),
        )
    return out


def without_outliers(values: Sequence[float]) -> list[float]:
    if len(values) < 4:
        return list(values)
    q1, _, q3 = quantiles(values, n=4, method="inclusive")
    spread = q3 - q1
    return [v for v in values if q1 - IQR * spread <= v <= q3 + IQR * spread]


def implied_per_share(metric: Metric, multiple: float, target: Figures) -> float | None:
    shares = target.get("shares")
    if not shares:
        return None
    denominator = {
        "ev_ebitda": "ebitda",
        "ev_sales": "revenue",
        "ev_ebit": "ebit",
        "price_earnings": "net_income",
    }[metric]
    base = target.get(denominator)
    if base is None or base <= 0:
        return None
    if metric == "price_earnings":
        return multiple * base / shares
    equity = multiple * base - (target.get("debt") or 0.0) + (target.get("cash") or 0.0)
    return equity / shares


def restore(verdicts: dict[str, Verdict]) -> dict[str, Verdict]:
    """Below five kept, the closest dropped candidates (fit 2 or more) fill the sample.

    A strict reading should not leave a thin sample when related companies exist; an
    unrelated one (fit 1) is never restored. Each restored peer says so in its reason.
    """
    kept = sum(v.keep for v in verdicts.values())
    dropped = sorted(
        (v for v in verdicts.values() if not v.keep and v.fit >= RESTORE_FIT),
        key=lambda v: -v.fit,
    )
    out = dict(verdicts)
    for v in dropped[: max(0, MIN_PEERS - kept)]:
        out[v.ticker] = v.model_copy(
            update={
                "keep": True,
                "reason": f"restored to reach {MIN_PEERS} peers (fit {v.fit}/5): {v.reason}"[:300],
            }
        )
    return out


class CompsAgent:
    def __init__(self, sec: SECClient, prices: PriceClient, llm: LocalLLM | None) -> None:
        self.sec, self.prices, self.llm = sec, prices, llm

    def review(self, target: str, industry: str, found: Sequence[Candidate]) -> dict[str, Verdict]:
        """The model's keep or drop per candidate; every candidate kept when it fails."""
        keep_all = {
            c.ticker: Verdict(ticker=c.ticker, keep=True, reason="kept: no model reading")
            for c in found
        }
        if self.llm is None or not found:
            return keep_all
        listing = "\n".join(
            f"- {c.ticker}: {c.name}; SIC {c.sic_description or 'unknown'}; revenue "
            f"${c.revenue / 1e9:,.1f}B"
            for c in found
        )
        prompt = (
            f"Target company: {target}, industry {industry}.\nCandidate peers:\n{listing}\n"
            "For each candidate, rate how comparable its business is for valuing the target "
            "(fit 5: same products, customers and economics; 3: related, same industry "
            "economics; 1: unrelated), say whether to keep it, and give a one-line reason."
        )
        try:
            reply = self.llm.generate(
                "comps", prompt, Verdicts, prompt_version=PROMPT_VERSION
            ).value
        except LLMError:
            return keep_all
        verdicts = {v.ticker.upper(): v for v in reply.verdicts}
        return restore({c.ticker: verdicts.get(c.ticker, keep_all[c.ticker]) for c in found})

    def find(
        self,
        ticker: str,
        industry: str,
        as_of: date,
        classification: Sequence[dict[str, str]],
        revenue: float,
    ) -> list[Candidate]:
        """Candidates in the industry and the size band, closest in size first."""
        year = as_of.year - 1 if as_of.month > 3 else as_of.year - 2
        return candidates(
            classification,
            industry,
            ticker,
            self.sec.get_ticker_map(),
            frame_revenues(self.sec, year),
            revenue,
            self.sec.get_company_profile,
        )

    def run(
        self,
        ticker: str,
        cik: str,
        industry: str,
        as_of: date,
        classification: Sequence[dict[str, str]],
        dcf_value: float | None,
        own: dict[str, Sourced[float]] | None = None,
    ) -> tuple[CompsResult, list[Candidate], dict[str, Verdict], Figures]:
        """Peers, multiples and the implied range; `own` fills the target's missing figures.

        A company whose current registrant is new (a holding reorganization) has few
        company facts of its own; the valuation's TTM figures then stand in.
        """
        target = figures(self.sec, self.prices, ticker, cik, as_of)
        if own:
            target.fill(own)
        revenue = target.get("revenue") or 0.0
        found = self.find(ticker, industry, as_of, classification, revenue)
        verdicts = self.review(ticker, industry, found)
        warnings: list[str] = []
        restored = [t for t, v in verdicts.items() if v.keep and v.reason.startswith("restored")]
        if restored:
            warnings.append(f"{', '.join(restored)}: weaker peers, restored to reach {MIN_PEERS}")
        peers: list[PeerMultiples] = []
        for c in found:
            verdict = verdicts[c.ticker]
            if not verdict.keep:
                continue
            f = figures(self.sec, self.prices, c.ticker, c.cik, as_of)
            m = multiples(f)
            if not m:
                warnings.append(
                    f"{c.ticker}: no multiple ({'; '.join(f.notes) or 'missing inputs'})"
                )
                continue
            peers.append(
                PeerMultiples(
                    ticker=c.ticker,
                    cik=c.cik,
                    as_of=as_of,
                    selection_rationale=f"{industry}; revenue {c.revenue / max(revenue, 1):.1f}x "
                    f"the target's; {verdict.reason}",
                    ev_ebitda=m.get("ev_ebitda"),
                    ev_sales=m.get("ev_sales"),
                    ev_ebit=m.get("ev_ebit"),
                    price_earnings=m.get("price_earnings"),
                )
            )
        if len(peers) < MIN_PEERS:
            warnings.append(f"only {len(peers)} peers with multiples (fewer than {MIN_PEERS})")
        summaries: list[MultipleSummary] = []
        implied: list[float] = []
        for metric in METRICS:
            raw = [getattr(p, metric).value for p in peers if getattr(p, metric) is not None]
            kept = without_outliers(raw)
            if len(raw) > len(kept):
                warnings.append(f"{metric}: {len(raw) - len(kept)} outliers removed")
            if not kept:
                continue
            q1, mid, q3 = (
                quantiles(kept, n=4, method="inclusive") if len(kept) >= 2 else [kept[0]] * 3
            )
            mid = median(kept)
            q1, q3 = min(q1, mid), max(q3, mid)
            if len(kept) < MIN_PEERS and not any("fewer" in w for w in warnings):
                warnings.append(f"{metric}: {len(kept)} peers only")
            summaries.append(
                MultipleSummary(
                    metric=metric,
                    sample_size=len(kept),
                    first_quartile=q1,
                    median=mid,
                    third_quartile=q3,
                )
            )
            value = implied_per_share(metric, mid, target)
            if value is not None and value > 0:
                implied.append(value)
        mid_value = median(implied) if implied else None
        result = CompsResult(
            as_of=as_of,
            peers=tuple(peers),
            summaries=tuple(summaries),
            implied_value_range=(min(implied), max(implied)) if implied and summaries else None,
            divergence_vs_dcf=dcf_value / mid_value - 1 if dcf_value and mid_value else None,
            calculation_ref="dcf_agent.agents.comps",
            warnings=tuple(warnings) or (() if len(peers) >= MIN_PEERS else ("few peers",)),
        )
        return result, found, verdicts, target
