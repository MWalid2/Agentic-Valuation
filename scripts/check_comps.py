"""Phase 5 steps 1-2 exit check: comps on the golden set.

For each company: the candidates (Damodaran industry, size band), the model's keep or
drop with reasons (or all kept with --no-model), the peers' multiples, quartiles after
outliers, the implied value per share at each median, divergence from the DCF value of
the last `check_value.py` run, warnings, and the provenance audit of every multiple.
The first run downloads the peers' SEC company facts and prices (then cached).
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from dcf_agent.agents.comps import CompsAgent
from dcf_agent.config import load_config
from dcf_agent.contracts.accounting import evidence_records
from dcf_agent.contracts.external import DataBundle
from dcf_agent.data.damodaran import DamodaranClient
from dcf_agent.data.prices import PriceClient
from dcf_agent.data.sec import SECClient
from dcf_agent.engine.audit import audit_records
from dcf_agent.llm import LocalLLM

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*")
    parser.add_argument("--bundles", type=Path, default=ROOT / "outputs/insights-cohort")
    parser.add_argument("--values", type=Path, default=ROOT / "outputs/value-check.json")
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/comps-check.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    config = load_config(ROOT)
    config = config.model_copy(
        update={"settings": config.settings.model_copy(update={"mock_llm": False})}
    )
    dcf = (
        {
            r["ticker"]: r["weighted"]
            for r in json.loads(args.values.read_text())["rows"]
            if r["output"] == "quick"
        }
        if args.values.exists()
        else {}
    )
    rows: list[dict[str, Any]] = []
    paths = sorted(args.bundles.glob("*.json"))
    if args.tickers:
        paths = [p for p in paths if p.stem in args.tickers]
    with (
        SECClient(config, offline=args.offline) as sec,
        PriceClient(config, offline=args.offline) as prices,
        DamodaranClient(config, offline=True) as damodaran,
        LocalLLM(config) as llm,
    ):
        classification, _ = damodaran.get_classification()
        agent = CompsAgent(sec, prices, None if args.no_model else llm)
        for path in paths:
            bundle = DataBundle.model_validate_json(path.read_text(encoding="utf-8"))
            request = bundle.request
            industry = bundle.industry.name if bundle.industry else None
            if industry is None:
                print(f"{request.ticker:5} no industry")
                continue
            began = time.monotonic()
            result, found, verdicts, _ = agent.run(
                request.ticker,
                request.cik,
                industry,
                request.valuation_date,
                classification,
                dcf.get(request.ticker),
            )
            audit = audit_records(evidence_records(result))
            row: dict[str, Any] = {
                "ticker": request.ticker,
                "industry": industry,
                "candidates": [c.ticker for c in found],
                "dropped": {t: v.reason for t, v in verdicts.items() if not v.keep},
                "peers": [p.ticker for p in result.peers],
                "medians": {s.metric: round(s.median, 2) for s in result.summaries},
                "implied": result.implied_value_range,
                "dcf": dcf.get(request.ticker),
                "divergence": result.divergence_vs_dcf,
                "warnings": list(result.warnings),
                "audit_problems": audit.problems[:3],
                "seconds": round(time.monotonic() - began, 1),
            }
            rows.append(row)
            implied = row["implied"]
            gap = row["divergence"]
            divergence = "-" if gap is None else f"{gap:+.0%}"
            print(
                f"{request.ticker:5} {industry[:28]:28} peers {len(row['peers'])}/"
                f"{len(row['candidates'])} {row['peers']} medians {row['medians']} implied "
                f"{'-' if not implied else f'{implied[0]:,.0f}-{implied[1]:,.0f}'} DCF "
                f"{row['dcf']} div {divergence} "
                f"audit {'clean' if not audit.problems else audit.problems[:1]} {row['seconds']}s",
                flush=True,
            )
            for ticker, reason in row["dropped"].items():
                print(f"      dropped {ticker}: {reason}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2, default=str))
    print(
        json.dumps(
            {
                "companies": len(rows),
                "five_or_more_peers": sum(len(r["peers"]) >= 5 for r in rows),
                "with_range": sum(bool(r["implied"]) for r in rows),
                "audit_clean": sum(not r["audit_problems"] for r in rows),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
