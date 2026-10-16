"""Validate the local RAGAS judge against exact retrieval truth.

On a fixed sample of development questions, the default retrieval returns four
passages; RAGAS `LLMContextRecall`, judged by the local qwen3:4b, says whether the
reference answer is supported by them. That verdict is compared with two exact
truths: strict (a passage holding a reference fact was retrieved) and lenient
(the fact's displayed number appears in a retrieved passage, as the same figure is
often repeated elsewhere). Low agreement means judge-scored metrics are reported
as diagnostic only.

The judge runs through Ollama's native API with a JSON output format: qwen3
reasons in free text otherwise, even with thinking turned off.
"""

import argparse
import hashlib
import json
import sys
import time
import warnings
from datetime import date
from pathlib import Path
from typing import Any

from dcf_agent.rag.index import FilingIndex
from dcf_agent.rag.queries import expand

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]


def reference_text(item: dict[str, Any]) -> str:
    value, unit = item["answer"], item["unit"]
    if unit == "pure":
        amount = f"{value:.2%}"
    elif unit == "USD":
        amount = f"${value / 1e6:,.1f} million"
    elif unit == "USD/shares":
        amount = f"${value:,.2f} per share"
    else:
        amount = f"{value:,.0f} {unit}"
    return f"{item['question'].rstrip('?')}: {amount}."


def judge() -> Any:
    from langchain_ollama import ChatOllama
    from ragas.llms import LangchainLLMWrapper

    chat = ChatOllama(
        model="qwen3:4b",
        temperature=0,
        reasoning=False,
        format="json",
        num_ctx=4096,
        num_predict=1024,
    )
    return LangchainLLMWrapper(chat)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=30)
    parser.add_argument(
        "--negatives",
        type=int,
        default=0,
        help="Also judge this many questions against another company's passages (truth: no)",
    )
    parser.add_argument("--index", type=Path, default=ROOT / "data/rag/index.sqlite")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/rag-judge-validation.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    from ragas import SingleTurnSample
    from ragas.metrics import LLMContextRecall

    gold = [
        json.loads(line)
        for line in (ROOT / "evals/rag/gold-xbrl.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    dev = sorted(
        (g for g in gold if g["split"] == "dev"),
        key=lambda g: hashlib.sha256(g["id"].encode()).hexdigest(),
    )[: args.sample]
    index = FilingIndex(args.index)
    accessions: dict[str, list[str]] = {}
    for cik, accession in index.db.execute("SELECT cik, accession FROM filings"):
        accessions.setdefault(cik, []).append(accession)
    metric = LLMContextRecall(llm=judge())
    rows = []
    others = sorted(accessions)
    cases = [(item, item["cik"]) for item in dev]
    # Negatives: the same question searched in another company's filings.
    cases += [
        (item, others[(others.index(item["cik"]) + 1) % len(others)])
        for item in dev[: args.negatives]
    ]
    for item, searched in cases:
        query = expand(item["question"], date.fromisoformat(item["period_end"]))
        hits = index.search(query, accessions[searched], k=4, mode="bm25", field=item["field"])
        references = index.chunk_ids_for_facts(item["accession"], item["reference_fact_ids"])
        shown = {
            f.shown
            for c in references
            for f in index.chunk(c).facts
            if f.fact_id in item["reference_fact_ids"] and f.shown
        }
        strict = any(h.chunk_id in references for h in hits)
        lenient = strict or any(s and s in h.text for s in shown for h in hits)
        if searched != item["cik"]:
            strict = lenient = False  # another company's filing cannot hold this answer
        sample = SingleTurnSample(
            user_input=item["question"],
            retrieved_contexts=[h.text for h in hits],
            reference=reference_text(item),
        )
        began = time.monotonic()
        try:
            score: float | None = float(metric.single_turn_score(sample))
            error = None
        except Exception as exc:  # a judge failure is recorded, not hidden
            score, error = None, f"{type(exc).__name__}: {str(exc)[:120]}"
        rows.append(
            {
                "id": item["id"],
                "negative": searched != item["cik"],
                "strict": strict,
                "lenient": lenient,
                "judge_score": score,
                "judge_supported": score is not None and score >= 0.5,
                "error": error,
                "seconds": round(time.monotonic() - began, 1),
            }
        )
        print(
            f"{item['id'][:60]:60} strict={strict!s:5} lenient={lenient!s:5} "
            f"judge={score} {rows[-1]['seconds']}s",
            flush=True,
        )
    judged = [r for r in rows if r["judge_score"] is not None]

    def agreement(truth: str) -> dict[str, Any]:
        table = {
            "both_yes": sum(r[truth] and r["judge_supported"] for r in judged),
            "both_no": sum(not r[truth] and not r["judge_supported"] for r in judged),
            "judge_yes_truth_no": sum(not r[truth] and r["judge_supported"] for r in judged),
            "judge_no_truth_yes": sum(r[truth] and not r["judge_supported"] for r in judged),
        }
        agree = table["both_yes"] + table["both_no"]
        return {"agreement": agree / len(judged) if judged else 0.0, **table}

    summary = {
        "sample": len(rows),
        "judged": len(judged),
        "judge_failures": len(rows) - len(judged),
        "strict": agreement("strict"),
        "lenient": agreement("lenient"),
        "median_seconds": sorted(r["seconds"] for r in rows)[len(rows) // 2] if rows else 0,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
