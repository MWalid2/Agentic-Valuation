"""Phase 6 step 4: time information answers from a finished session (cache as a user has it).

For each company's latest batch session, a few follow-up questions are asked; each
reuses what the session holds (data, WACC, forecast, valuation) and is timed end to
end, router to answer. Target: under 60 seconds each.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from dcf_agent.config import load_config
from dcf_agent.graph.services import LiveServices
from dcf_agent.graph.workflow import build_graph, open_checkpointer, start
from dcf_agent.report.trace import sessions

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = (
    "what is the cost of capital?",
    "what was revenue over the last five years?",
    "where does the debt figure come from?",
    "what are the bull and bear cases?",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", default=["KO", "AAPL", "NKE"])
    parser.add_argument("--db", type=Path, default=ROOT / "outputs/batch/sessions.sqlite")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/answer-times.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    config = load_config(ROOT)
    config = config.model_copy(
        update={"settings": config.settings.model_copy(update={"mock_llm": False})}
    )
    known = sessions(args.db)
    rows = []
    saver = open_checkpointer(args.db)
    try:
        with LiveServices(config, date.today(), offline=True) as live:
            graph = build_graph(live.services(), saver)
            for ticker in args.tickers:
                session = next(
                    (s for s in reversed(known) if s.startswith(f"batch-{ticker}-")), None
                )
                if session is None:
                    continue
                for question in QUESTIONS:
                    began = time.monotonic()
                    state = start(graph, session, question)
                    seconds = round(time.monotonic() - began, 1)
                    answer = state.get("answer") or ""
                    rows.append(
                        {
                            "ticker": ticker,
                            "question": question,
                            "intent": state["request"].intent,
                            "seconds": seconds,
                            "cited": "[1]" in answer,
                            "status": state.get("status"),
                        }
                    )
                    print(f"{ticker:5} {seconds:6.1f}s {rows[-1]['intent']} {question}", flush=True)
    finally:
        saver.conn.close()
    times = sorted(r["seconds"] for r in rows)
    summary = {
        "answers": len(rows),
        "under_60s": sum(t < 60 for t in times),
        "median": times[len(times) // 2] if times else None,
        "max": times[-1] if times else None,
        "cited": sum(r["cited"] for r in rows),
    }
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
