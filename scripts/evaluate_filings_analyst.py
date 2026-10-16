"""Filings Analyst on the gold set: model reading (XBRL hidden) and the XBRL path.

For debt, lease and share-count questions, the analyst reads each answer with
the XBRL shortcut disabled, so the model must find and point at the value. Each
reading is classified: correct with high confidence; correct but flagged; wrong
but flagged; not found; or wrong with high confidence (the dangerous case). The
XBRL path is checked on the same questions without the model.
"""

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from dcf_agent.agents.filings import BY_ID, FilingsAnalyst, close
from dcf_agent.config import load_config
from dcf_agent.llm import LocalLLM
from dcf_agent.rag.index import FilingIndex

ROOT = Path(__file__).resolve().parents[1]


def outcome(reading: Any, answer: float) -> str:
    if reading.status == "not_found" or reading.value is None:
        return "not_found"
    right = close(reading.value.value, answer) or abs(reading.value.value - answer) < 1e-6
    if right:
        return "correct_high" if reading.confidence == "high" else "correct_low"
    return "wrong_high" if reading.confidence == "high" else "wrong_low"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--sample", type=int, default=0, help="0: every question")
    parser.add_argument("--index", type=Path, default=ROOT / "data/rag/index.sqlite")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/filings-analyst-eval.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    gold = [
        json.loads(line)
        for line in (ROOT / "evals/rag/gold-xbrl.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    items = sorted(
        (g for g in gold if g["split"] == args.split and g["concept"] in BY_ID),
        key=lambda g: hashlib.sha256(g["id"].encode()).hexdigest(),
    )
    if args.sample:
        items = items[: args.sample]
    config = load_config(ROOT)
    config = config.model_copy(
        update={"settings": config.settings.model_copy(update={"mock_llm": False})}
    )
    index = FilingIndex(args.index)
    accessions: dict[str, list[str]] = {}
    for cik, accession in index.db.execute("SELECT cik, accession FROM filings"):
        accessions.setdefault(cik, []).append(accession)
    rows = []
    with LocalLLM(config) as llm:
        analyst = FilingsAnalyst(index, llm)
        for item in items:
            spec = BY_ID[item["concept"]]
            period_end = date.fromisoformat(item["period_end"])
            began = time.monotonic()
            reading = analyst.read(spec, accessions[item["cik"]], period_end, use_xbrl=False)
            seconds = time.monotonic() - began
            xbrl = analyst.tagged(spec, item["accession"], period_end)
            row = {
                "id": item["id"],
                "field": item["field"],
                "answer": item["answer"],
                "read": reading.value.value if reading.value else None,
                "confidence": reading.confidence,
                "outcome": outcome(reading, item["answer"]),
                "reasons": list(reading.reasons),
                "cell": reading.cell,
                "calls": reading.model_calls,
                "seconds": round(seconds, 1),
                "xbrl_correct": xbrl is not None
                and xbrl.value is not None
                and close(xbrl.value.value, item["answer"]),
            }
            rows.append(row)
            print(
                f"{item['id'][:58]:58} {row['outcome']:12} read={row['read']} "
                f"answer={item['answer']} {row['seconds']}s {row['reasons'][:1]}",
                flush=True,
            )
    counts = Counter(r["outcome"] for r in rows)
    n = len(rows)
    summary = {
        "split": args.split,
        "questions": n,
        "outcomes": dict(counts),
        "correct": (counts["correct_high"] + counts["correct_low"]) / n if n else 0,
        "correct_high": counts["correct_high"] / n if n else 0,
        "confident_wrong": counts["wrong_high"] / n if n else 0,
        "xbrl_path_correct": sum(r["xbrl_correct"] for r in rows) / n if n else 0,
        "by_field": {
            f: {
                "n": sum(r["field"] == f for r in rows),
                "correct": sum(
                    r["field"] == f and r["outcome"].startswith("correct") for r in rows
                ),
            }
            for f in sorted({r["field"] for r in rows})
        },
        "median_seconds": sorted(r["seconds"] for r in rows)[n // 2] if n else 0,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
