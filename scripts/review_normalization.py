"""Step 6 exit check: the Normalization Agent's proposals, written for human review.

For each company (a bundle saved by `check_insights.py`, rebuilt offline with the
current code), the agent proposes EBIT adjustments for the last five fiscal years
and the TTM. Every candidate is listed, adjusted or not, with its amounts by year,
the rule outcome, the model's label and reason, and the passage it relied on, so a
reviewer can mark false positives (adjustments that should not be made) and misses.
Acceptance: at most one false positive per company on five companies.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dcf_agent.agents.normalizer import NormalizationAgent, Proposal
from dcf_agent.config import load_config
from dcf_agent.contracts.external import DataBundle
from dcf_agent.data.sec import SECClient
from dcf_agent.llm import LocalLLM
from dcf_agent.rag.filings import filing_texts
from dcf_agent.rag.index import FilingIndex
from dcf_agent.tools.data import periodic_filings

ROOT = Path(__file__).resolve().parents[1]
COMPANIES = ("KO", "NKE", "BA", "CAT", "DE")


def millions(value: float) -> str:
    return f"{value / 1e6:,.0f}"


def section(ticker: str, bundle: DataBundle, proposal: Proposal) -> list[str]:
    raw = bundle.raw
    assert raw is not None
    latest = sorted(raw.annual, key=lambda p: p.period_end)[-1]
    ebit = latest.values["ebit"].value
    lines = [
        f"## {bundle.request.company_name} ({ticker})",
        "",
        f"Latest fiscal-year EBIT {millions(ebit)}M ({latest.period_end}). Notes: "
        + "; ".join(proposal.notes),
        "",
    ]
    for n, d in enumerate(proposal.decisions, 1):
        c = d.candidate
        amounts = ", ".join(
            f"{end:%Y-%m} {millions(v.value)}" for end, v in sorted(c.amounts.items())
        )
        where = "income statement line" if c.on_face else "tagged in the notes"
        sign = "income" if c.gain else "charge"
        lines += [
            f"### {ticker}-{n}. {c.label} ({where}, {c.category})",
            "",
            f"- Amounts (USD millions; {sign} when positive): {amounts}",
            f"- Present in {d.years_present} of the last five years: "
            f"{'recurring' if d.rule_recurring else 'not recurring'} by rule",
            "- Model: "
            + (
                f"recurring={d.label.recurring}, "
                f"passage {d.label.passage or 'none'}: {d.label.reason}"
                if d.label
                else "no reading"
            ),
            f"- **Proposal: {'ADJUST' if d.adjust else 'no adjustment'}**"
            + (" (ambiguous)" if d.ambiguous else "")
            + f", confidence {d.confidence}. {d.rationale}",
        ]
        for a in d.adjustments:
            share = a.materiality_pct_ebit
            lines.append(
                f"  - {a.period_end}: EBIT {a.direction} {millions(a.amount.value)}M"
                + (f" ({share:.1%} of that year's EBIT)" if share is not None else "")
                + (" - Gate 1" if a.requires_review else "")
            )
        if d.passage:
            excerpt = " ".join(d.passage.split())[:500]
            lines.append(f"- Passage ({d.citation.section if d.citation else ''}): > {excerpt}")
        lines += [
            "- Your verdict: [ ] correct  [ ] false positive  [ ] missed adjustment  Comment: ",
            "",
        ]
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", default=list(COMPANIES))
    parser.add_argument("--bundles", type=Path, default=ROOT / "outputs/insights-cohort")
    parser.add_argument("--out", type=Path, default=ROOT / "evals/normalization/review.md")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    config = load_config(ROOT)
    config = config.model_copy(
        update={"settings": config.settings.model_copy(update={"mock_llm": False})}
    )
    index = FilingIndex(config.settings.project_root / "data/rag/index.sqlite")
    report = [
        "# Normalization Agent: proposals for review",
        "",
        "For each candidate, tick one verdict. A false positive is an adjustment that should "
        "not be made (a recurring cost stripped, a wrong amount or direction); a missed "
        "adjustment is a one-off the agent left in EBIT. Acceptance: at most one false "
        "positive per company.",
        "",
    ]
    summary: dict[str, Any] = {}
    with SECClient(config, offline=True) as sec, LocalLLM(config) as llm:
        agent = NormalizationAgent(sec, index, llm)
        for ticker in args.tickers:
            bundle = DataBundle.model_validate_json(
                (args.bundles / f"{ticker}.json").read_text(encoding="utf-8")
            )
            if bundle.raw is None:
                continue
            request = bundle.request
            source, annual = periodic_filings(sec, request.cik, request.valuation_date, ("10-K",))
            texts = filing_texts(sec, request.cik, request.valuation_date)
            for text in texts:
                index.add(text, request.cik)
            proposal = agent.propose(bundle.raw, source, annual, [t.accession for t in texts])
            report += section(ticker, bundle, proposal)
            summary[ticker] = {
                "candidates": len(proposal.decisions),
                "adjusted": sum(d.adjust for d in proposal.decisions),
                "adjustments": len(proposal.adjustments),
                "gate_1": sum(a.requires_review for a in proposal.adjustments),
            }
            print(ticker, summary[ticker], flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Review file: {args.out}")


if __name__ == "__main__":
    main()
