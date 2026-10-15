"""Live local model evaluation of dated SEC debt interpretation."""

import argparse
import json
import time
from datetime import date
from pathlib import Path

from dcf_agent.agents.edgar import EDGARAgent
from dcf_agent.config import load_config
from dcf_agent.data.sec import SECClient
from dcf_agent.llm import LocalLLM
from dcf_agent.tools.edgar import EDGARTools


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline-sec", action="store_true", help="Replay SEC snapshots; LLM remains live"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = load_config(root)
    config = config.model_copy(
        update={
            "settings": config.settings.model_copy(update={"mock_llm": False}),
            "models": config.models.model_copy(
                update={
                    "runtime": config.models.runtime.model_copy(update={"response_cache": False})
                }
            ),
        }
    )
    cases = (
        ("Coca-Cola", "21344", date(2025, 12, 31), 45_492_000_000.0),
        ("Nike", "320187", date(2026, 5, 31), 7_942_000_000.0),
        ("Apple", "320193", date(2025, 9, 27), 98_657_000_000.0),
    )
    records = []
    with SECClient(config, offline=args.offline_sec) as client, LocalLLM(config) as llm:
        for company, cik, period, expected in cases:
            start = time.monotonic()
            tools = EDGARTools(client, cik, date(2026, 10, 11), period_end=period)
            result = EDGARAgent(llm).run(tools)
            correct = result.total is not None and abs(result.total.value - expected) <= 1.0
            record = {
                "company": company,
                "expected": expected,
                "correct": correct,
                "seconds": time.monotonic() - start,
                "result": result.model_dump(mode="json"),
            }
            records.append(record)
            print(
                f"{company}: status={result.status} "
                f"total={result.total.value if result.total else None} "
                f"correct={correct} tools={result.tool_calls}",
                flush=True,
            )
    destination = root / "data/edgar-agent/evaluation.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(records, indent=2), encoding="utf-8")
    if not all(r["correct"] for r in records):
        raise SystemExit("Some debt cases need review; inspect data/edgar-agent/evaluation.json")


if __name__ == "__main__":
    main()
