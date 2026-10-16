"""Retrieval on the XBRL-labelled gold set: hit@k and RAGAS ID-based context metrics.

Each company's point-in-time filings (latest 10-K and the 10-Qs after it) are
indexed; every gold question is searched in those filings; a question is a hit
when a chunk holding one of its reference facts is among the top k. RAGAS
`IDBasedContextRecall` and `IDBasedContextPrecision` are computed by the ragas
library on the same retrieved and reference chunk IDs. Tune on `--split dev`;
report `--split test`.
"""

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from ragas import SingleTurnSample
from ragas.metrics import IDBasedContextPrecision, IDBasedContextRecall

from dcf_agent.config import load_config
from dcf_agent.data.sec import SECClient
from dcf_agent.rag.filings import filing_texts
from dcf_agent.rag.index import EMBEDDING_MODEL, FilingIndex
from dcf_agent.rag.queries import expand

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--modes", default="bm25", help="Comma list: bm25, dense, hybrid")
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument(
        "--expand", action="store_true", help="Add filing vocabulary and fiscal-year labels"
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--index", type=Path, default=ROOT / "data/rag/index.sqlite")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/retrieval-eval.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    modes = args.modes.split(",")
    gold = [
        json.loads(line)
        for line in (ROOT / "evals/rag/gold-xbrl.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    gold = [g for g in gold if g["split"] == args.split]
    embedder = None
    if any(m in ("dense", "hybrid") for m in modes):
        from fastembed import TextEmbedding

        embedder = TextEmbedding(EMBEDDING_MODEL)
    index = FilingIndex(args.index, embedder)
    accessions: dict[str, list[str]] = {}
    began = time.monotonic()
    with SECClient(load_config(ROOT), offline=True) as sec:
        for cik in dict.fromkeys(g["cik"] for g in gold):
            texts = filing_texts(sec, cik, args.as_of)
            for text in texts:
                index.add(text, cik)
            accessions[cik] = [t.accession for t in texts]
            embedded = index.embed(accessions[cik]) if embedder else 0
            if embedded:
                print(f"embedded {embedded} chunks for {cik}", flush=True)
    print(f"indexed {len(accessions)} companies in {time.monotonic() - began:.0f}s", flush=True)
    recall_metric, precision_metric = IDBasedContextRecall(), IDBasedContextPrecision()
    report: dict[str, Any] = {"split": args.split, "k": args.k, "questions": len(gold)}
    for mode in modes:
        for targeted in (False, True):
            name = f"{mode}{'+targeted' if targeted else ''}"
            rows = []
            for item in gold:
                references = index.chunk_ids_for_facts(
                    item["accession"], item["reference_fact_ids"]
                )
                query = item["question"]
                if args.expand:
                    query = expand(query, date.fromisoformat(item["period_end"]))
                hits = index.search(
                    query,
                    accessions[item["cik"]],
                    k=args.k,
                    mode=mode,
                    field=item["field"] if targeted else None,
                )
                retrieved = [h.chunk_id for h in hits]
                sample = SingleTurnSample(
                    retrieved_context_ids=list[str | int](retrieved),
                    reference_context_ids=list[str | int](sorted(references)),
                )
                rows.append(
                    {
                        "id": item["id"],
                        "field": item["field"],
                        "hit": bool(references & set(retrieved)),
                        "rank": next(
                            (i + 1 for i, c in enumerate(retrieved) if c in references), None
                        ),
                        "ragas_recall": recall_metric.single_turn_score(sample),
                        "ragas_precision": precision_metric.single_turn_score(sample),
                        "references": len(references),
                    }
                )
            by_field: dict[str, list[bool]] = defaultdict(list)
            for row in rows:
                by_field[row["field"]].append(row["hit"])
            summary = {
                "hit_at_k": sum(r["hit"] for r in rows) / len(rows),
                "ragas_id_context_recall": statistics.mean(r["ragas_recall"] for r in rows),
                "ragas_id_context_precision": statistics.mean(r["ragas_precision"] for r in rows),
                "mrr": statistics.mean(1 / r["rank"] if r["rank"] else 0 for r in rows),
                "by_field": {f: round(sum(h) / len(h), 3) for f, h in sorted(by_field.items())},
                "misses": [r["id"] for r in rows if not r["hit"]],
            }
            report[name] = summary
            print(
                f"{name:16} hit@{args.k} {summary['hit_at_k']:.3f}  "
                f"RAGAS recall {summary['ragas_id_context_recall']:.3f}  "
                f"precision {summary['ragas_id_context_precision']:.3f}  "
                f"MRR {summary['mrr']:.3f}  {summary['by_field']}",
                flush=True,
            )
    index.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
