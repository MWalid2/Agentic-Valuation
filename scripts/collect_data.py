"""Collect SEC, prices, FRED and Damodaran through the Data Agent's bounded tools.

Only the ticker and valuation date are required: the CIK and name come from the
SEC ticker list, the fiscal period from the latest 10-K, and the industry from
Damodaran's company file (override it with --industry).
"""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from dcf_agent.agents.data import DataAgent
from dcf_agent.agents.lines import MappingStore
from dcf_agent.agents.snapshot import SnapshotAgent, SnapshotResult
from dcf_agent.config import load_config
from dcf_agent.contracts.inputs import ValuationRequest
from dcf_agent.data.damodaran import DamodaranClient
from dcf_agent.data.fred import FREDClient
from dcf_agent.data.prices import PriceClient
from dcf_agent.data.sec import SECClient, normalize_cik
from dcf_agent.llm import LocalLLM
from dcf_agent.tools.data import DataTools


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--cik", help="Default: from the SEC ticker list")
    parser.add_argument("--name", help="Default: the SEC registrant name")
    parser.add_argument("--industry", help="Exact Damodaran US industry label (default: mapped)")
    parser.add_argument("--country", default="United States")
    parser.add_argument("--price-start", type=date.fromisoformat)
    parser.add_argument(
        "--offline", action="store_true", help="External sources use saved responses"
    )
    parser.add_argument(
        "--interpret",
        action="store_true",
        help="Also run the local-model SEC snapshot as a cross-check (minutes on a CPU)",
    )
    parser.add_argument(
        "--sec-snapshot", type=Path, help="Cross-check against a saved check_snapshot_agent result"
    )
    parser.add_argument("--out", type=Path, default=Path("outputs/data-bundle.json"))
    args = parser.parse_args()
    config = load_config(Path(__file__).resolve().parents[1])
    saved = None
    if args.sec_snapshot:
        record = json.loads(args.sec_snapshot.read_text(encoding="utf-8"))
        saved = SnapshotResult.model_validate_json(json.dumps(record.get("result", record)))
    elif args.interpret:
        if args.offline:
            parser.error("--interpret needs the local model; use --sec-snapshot offline")
        roles = dict(config.models.roles)
        roles["filings"] = roles["filings"].model_copy(update={"num_predict": 512})
        config = config.model_copy(
            update={
                "settings": config.settings.model_copy(update={"mock_llm": False}),
                "models": config.models.model_copy(update={"roles": roles}),
            }
        )
    with (
        SECClient(config, offline=args.offline) as sec,
        FREDClient(config, offline=args.offline) as fred,
        DamodaranClient(config, offline=args.offline) as damodaran,
        PriceClient(config, offline=args.offline) as prices,
        LocalLLM(config) as llm,
    ):
        ticker = args.ticker.upper()
        cik = normalize_cik(args.cik) if args.cik else sec.get_ticker_map().get(ticker)
        if cik is None:
            parser.error(f"{ticker} is not in the SEC ticker list; pass --cik")
        name = args.name or sec.get_company_profile(cik)["name"] or ticker
        request = ValuationRequest(
            ticker=ticker,
            cik=cik,
            company_name=name,
            valuation_date=args.as_of,
            mode="info",
            question="Collect company, market, macro, and industry inputs",
        )
        tools = DataTools(
            request,
            sec=sec,
            snapshot_agent=SnapshotAgent(llm),
            fred=fred,
            damodaran=damodaran,
            prices=prices,
            snapshot_result=saved,
            mappings=MappingStore(config.settings.project_root / "data/mappings"),
        )
        result = DataAgent().run(
            tools,
            price_start=args.price_start or args.as_of - timedelta(days=730),
            industry=args.industry,
            country=args.country,
            interpret=args.interpret,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(f"Saved {args.out}; {len(result.tool_calls)} tool calls")
    if result.industry is not None:
        print(f"Industry: {result.industry.name} ({result.industry.method})")
    print("Missing/blocked: " + (", ".join(result.quality.missing_fields) or "none"))
    for check in result.quality.checks:
        if check.status != "passed":
            print(f"{check.severity}: {check.field}: {check.message}")
    if result.quality.has_blocking_issues:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
