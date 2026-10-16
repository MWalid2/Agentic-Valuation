"""Step 5 exit check: filing insights in the valuation, on the WACC reference cohort.

Each company's bundle is rebuilt offline with the current code, through the same
services as the graph (so the filing insights are read as in a session). Then:

- diluted shares (cover-page shares plus unvested units plus options by the
  treasury-stock method at the market price) against the company's own diluted
  EPS denominator for its latest period, and the cover count alone against it;
  loss periods (diluted equal to basic: dilution is antidilutive) are not compared;
- readiness before (the saved bundle) and after (Deere's captive-finance split);
- the WACC with the filing's marginal tax rate against the 25% policy.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from dcf_agent.config import load_config
from dcf_agent.contracts.external import DataBundle
from dcf_agent.data.sec import SECError
from dcf_agent.engine.assemble import AssemblyError, assemble, shares_of
from dcf_agent.engine.dcf import dilute
from dcf_agent.engine.wacc import WaccError, compute_wacc
from dcf_agent.graph.services import MARGINAL_TAX, LiveServices
from dcf_agent.rag.filings import filing_texts
from dcf_agent.rag.index import FilingIndex
from dcf_agent.rag.insights import Filing, load

ROOT = Path(__file__).resolve().parents[1]
DILUTED = "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"
BASIC = "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic"
TOLERANCE = 0.03


def eps_shares(filings: list[Filing], concept: str, cover: float) -> tuple[float, str] | None:
    """The latest period's weighted-average count (shortest period at the latest end).

    A count tagged without its scale (e.g. 711.1 for 711.1 million) is rescaled
    when that brings it within 10% of the cover count, and the note says so.
    """
    for filing in filings:
        found = [
            f
            for _, f in filing.facts(concept)
            if not f.members and f.end == filing.period_end and f.start is not None
        ]
        if found:
            fact = min(found, key=lambda f: filing.period_end - (f.start or date.min))
            value = fact.value or 0.0
            for scale in (1e3, 1e6):
                if value and abs(value * scale / cover - 1) < 0.1 and abs(value / cover - 1) > 0.5:
                    return value * scale, f"{filing.period_end}; tagged without scale x{scale:g}"
            return value, str(filing.period_end)
    return None


def wacc_of(bundle: DataBundle, filings_tax: bool) -> float | None:
    try:
        inputs = assemble(bundle, marginal_tax=MARGINAL_TAX, filings_tax=filings_tax).wacc_inputs
        return compute_wacc(inputs).wacc.value
    except (AssemblyError, WaccError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*")
    parser.add_argument("--saved", type=Path, default=ROOT / "outputs/wacc-cohort")
    parser.add_argument("--bundles", type=Path, default=ROOT / "outputs/insights-cohort")
    parser.add_argument("--today", type=date.fromisoformat, default=date(2026, 10, 15))
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/insights-check.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    config = load_config(ROOT)
    index = FilingIndex(config.settings.project_root / "data/rag/index.sqlite")
    args.bundles.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    paths = sorted(args.saved.glob("*.json"))
    if args.tickers:
        paths = [p for p in paths if p.stem in args.tickers]
    with LiveServices(config, args.today, offline=True) as live:
        for path in paths:
            saved = DataBundle.model_validate_json(path.read_text(encoding="utf-8"))
            request = saved.request
            row: dict[str, Any] = {"ticker": request.ticker}
            try:
                bundle, _ = live.collect(request, saved.industry.name if saved.industry else None)
                texts = filing_texts(live.sec, request.cik, request.valuation_date)
            except SECError as error:
                row["error"] = f"unavailable offline: {error}"
                rows.append(row)
                print(f"{request.ticker:5} {row['error']}", flush=True)
                continue
            (args.bundles / path.name).write_text(bundle.model_dump_json(indent=1), "utf-8")
            insights = bundle.filing_insights
            filings = load(index, [t.accession for t in texts])
            before = {k: r.status for k, r in saved.readiness.items()}
            after = {k: r.status for k, r in bundle.readiness.items()}
            row.update(
                {
                    "readiness_before": before,
                    "readiness_after": after,
                    "insight_notes": [
                        c.message for c in bundle.quality.checks if c.field == "filings"
                    ],
                    "securities": [
                        {
                            "id": s.security_id,
                            "count": s.count.value,
                            "strike": s.strike_price.value if s.strike_price else None,
                        }
                        for s in (insights.dilutive_securities if insights else ())
                    ],
                    "lease_rate": next(
                        (
                            x.discount_rate.value
                            for x in (insights.lease_liabilities if insights else ())
                            if x.discount_rate
                        ),
                        None,
                    ),
                    "marginal_tax": next(
                        (
                            x.amount.value
                            for x in (insights.tax_items if insights else ())
                            if x.amount
                        ),
                        None,
                    ),
                    "captive_items": len(insights.captive_finance) if insights else 0,
                    "wacc_policy_tax": wacc_of(bundle, filings_tax=False),
                    "wacc_filing_tax": wacc_of(bundle, filings_tax=True),
                }
            )
            market = bundle.market
            if market is not None:
                price = market.close.price.value
                cover = market.shares_outstanding.value
                diluted = eps_shares(filings, DILUTED, cover)
                basic = eps_shares(filings, BASIC, cover)
                try:
                    shares = assemble(bundle, marginal_tax=MARGINAL_TAX).shares
                except AssemblyError:  # blocked: the shares alone
                    shares = shares_of(bundle, cover)[0]
                ours = dilute(price * cover, shares).diluted_shares
                row.update({"price": price, "cover_shares": cover, "our_diluted": ours})
                if diluted is not None:
                    loss = basic is not None and abs(diluted[0] - basic[0]) < 1
                    row.update(
                        {
                            "eps_diluted": diluted[0],
                            "eps_basic": basic[0] if basic else None,
                            "eps_period": diluted[1],
                            "antidilutive_period": loss,
                            "gap_ours": ours / diluted[0] - 1,
                            "gap_cover_only": cover / diluted[0] - 1,
                            "our_dilution": ours - cover,
                            "eps_dilution": diluted[0] - basic[0] if basic else None,
                        }
                    )
            rows.append(row)
            gap = row.get("gap_ours")
            print(
                f"{request.ticker:5} ours {row.get('our_diluted', 0) / 1e6:>9,.1f}M EPS "
                f"{row.get('eps_diluted', 0) / 1e6:>9,.1f}M gap "
                f"{'-' if gap is None else f'{gap:+.2%}'} (cover only "
                f"{row.get('gap_cover_only', 0):+.2%})"
                f"{' loss' if row.get('antidilutive_period') else ''}"
                f"; dilution {row.get('our_dilution', 0) / 1e6:,.1f}M vs "
                f"{(row.get('eps_dilution') or 0) / 1e6:,.1f}M; tax {row['marginal_tax']}; "
                f"lease {row['lease_rate']}; WACC {row['wacc_policy_tax']} -> "
                f"{row['wacc_filing_tax']}; wacc {before.get('wacc')} -> {after.get('wacc')}, "
                f"bridge {before.get('equity_bridge')} -> {after.get('equity_bridge')}",
                flush=True,
            )
    compared = [r for r in rows if "gap_ours" in r and not r["antidilutive_period"]]
    summary = {
        "companies": len(rows),
        "compared": len(compared),
        "tolerance": TOLERANCE,
        "within_tolerance": sum(abs(r["gap_ours"]) <= TOLERANCE for r in compared),
        "within_tolerance_cover_only": sum(abs(r["gap_cover_only"]) <= TOLERANCE for r in compared),
        "mean_abs_gap": sum(abs(r["gap_ours"]) for r in compared) / len(compared)
        if compared
        else 0,
        "mean_abs_gap_cover_only": sum(abs(r["gap_cover_only"]) for r in compared) / len(compared)
        if compared
        else 0,
        "valuable_before": [
            r["ticker"]
            for r in rows
            if all(
                r.get("readiness_before", {}).get(k) != "blocked" for k in ("wacc", "equity_bridge")
            )
        ],
        "valuable_after": [
            r["ticker"]
            for r in rows
            if r.get("readiness_after")
            and all(r["readiness_after"].get(k) != "blocked" for k in ("wacc", "equity_bridge"))
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
