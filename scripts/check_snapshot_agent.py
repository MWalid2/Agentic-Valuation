"""Uncached local-model evaluation of full annual snapshots against SEC filings."""

import argparse
import json
import time
from datetime import date
from pathlib import Path

from dcf_agent.agents.snapshot import SnapshotAgent
from dcf_agent.config import load_config
from dcf_agent.data.sec import SECClient
from dcf_agent.llm import LocalLLM
from dcf_agent.tools.edgar import EDGARTools


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-sec", action="store_true")
    parser.add_argument("--company", choices=("Apple", "Coca-Cola", "Nike"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_config(root)
    roles = dict(config.models.roles)
    roles["filings"] = roles["filings"].model_copy(update={"num_predict": 512})
    config = config.model_copy(
        update={
            "settings": config.settings.model_copy(update={"mock_llm": False}),
            "models": config.models.model_copy(
                update={
                    "roles": roles,
                    "runtime": config.models.runtime.model_copy(update={"response_cache": False}),
                }
            ),
        }
    )
    references = json.loads((root / "tests/fixtures/sec/snapshot-reference.json").read_text())
    records = []
    with SECClient(config, offline=args.offline_sec) as client, LocalLLM(config) as llm:
        for case in references:
            if args.company and case["company"] != args.company:
                continue
            start = time.monotonic()
            tools = EDGARTools(
                client,
                case["cik"],
                date(2026, 10, 11),
                period_end=date.fromisoformat(case["period_end"]),
            )
            result = SnapshotAgent(llm).run(
                tools,
                period_start=date.fromisoformat(case["period_start"]),
                fiscal_year=case["fiscal_year"],
            )
            comparisons = {
                concept: field.amount is not None
                and abs(field.amount.value - case["values"][concept]) <= 1.0
                for concept, field in result.snapshot.fields.items()
            }
            record = {
                "company": case["company"],
                "model": roles["filings"].model,
                "response_cache": False,
                "seconds": time.monotonic() - start,
                "shares_date_matches": str(result.snapshot.fields["shares_outstanding"].observed_on)
                == case["shares_observed_on"],
                "comparisons": comparisons,
                "result": result.model_dump(mode="json"),
            }
            records.append(record)
            print(
                f"{case['company']}: {sum(comparisons.values())}/11 matches, "
                f"balance={result.balance_check}",
                flush=True,
            )
            destination = root / "data/edgar-agent/snapshots"
            destination.mkdir(parents=True, exist_ok=True)
            (destination / (case["company"] + ".json")).write_text(
                json.dumps(record, indent=2), encoding="utf-8"
            )
    if not all(
        all(r["comparisons"].values())
        and r["result"]["balance_check"] == "passed"
        and r["shares_date_matches"]
        for r in records
    ):
        raise SystemExit("Some fields need review; inspect data/edgar-agent/snapshots")


if __name__ == "__main__":
    main()
