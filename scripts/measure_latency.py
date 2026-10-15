"""CPU latency of each chat stage, with the model response cache off.

Every model call runs (no replay). The first call of each model includes loading
it into memory and is reported as "cold"; later calls are "warm". Collection
replays saved source responses (--offline), so it measures this machine's
processing, not the network.
"""

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from dcf_agent.agents.answer import render
from dcf_agent.agents.wacc import WaccAgent
from dcf_agent.config import load_config
from dcf_agent.graph.services import MARGINAL_TAX, LiveServices
from dcf_agent.graph.workflow import build_graph, open_checkpointer, resume, start

ROOT = Path(__file__).resolve().parents[1]


def timed(fn: Callable[[], Any], runs: int) -> tuple[list[float], Any]:
    seconds, result = [], None
    for _ in range(runs):
        began = time.monotonic()
        result = fn()
        seconds.append(time.monotonic() - began)
    return seconds, result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--today", type=date.fromisoformat, default=date(2026, 10, 15))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/latency.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    config = load_config(ROOT)
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
    rows: list[dict[str, Any]] = []

    def record(stage: str, seconds: list[float], note: str = "") -> None:
        rows.append(
            {
                "stage": stage,
                "cold": round(seconds[0], 2),
                "warm_median": round(statistics.median(seconds[1:] or seconds), 2),
                "runs": len(seconds),
                "note": note,
            }
        )
        print(f"{stage:48} cold {seconds[0]:7.2f}s  warm {rows[-1]['warm_median']:7.2f}s  {note}")

    with LiveServices(config, args.today, offline=True) as live:
        router = live.router
        n = args.runs + 1
        seconds, _ = timed(lambda: router.route("Coca-Cola, 2026-10-11, what WACC?"), n)
        record("Router, rule-handled message", seconds, "no model call")
        seconds, route = timed(lambda: router.route("How much is Snowflake worth today?"), n)
        record("Router, free-form sentence", seconds, "qwen3:1.7b extracts the company")
        seconds, _ = timed(lambda: router.classify_intent("how does it stack up?"), n)
        record("Router, question no pattern recognizes", seconds, "qwen3:1.7b picks the intent")

        ko = router.route("KO, 2026-10-11").request("info")
        request = ko.model_copy(update={"plan": ("data", "wacc", "answer")})
        seconds, (bundle, _) = timed(lambda: live.collect(request, None), 2)
        record("Data collection (saved responses)", seconds, "SEC, prices, FRED, Damodaran")
        agent = WaccAgent(live.prices.get_price_history, live.llm, marginal_tax=MARGINAL_TAX)
        seconds, (wacc, _) = timed(lambda: agent.run(bundle), n)
        record("WACC Agent decision", seconds, "regressions + qwen3:4b choice")
        state = {
            "request": request.model_copy(update={"intent": "I3"}),
            "data": bundle,
            "wacc": wacc,
        }
        seconds, _ = timed(lambda: render(state), n)
        record("Answer rendering", seconds, "templates, no model")

        checkpoints = ROOT / "outputs/latency-checkpoints.sqlite"
        checkpoints.unlink(missing_ok=True)
        saver = open_checkpointer(checkpoints)
        graph = build_graph(live.services(), saver)
        began = time.monotonic()
        start(graph, "latency", "Coca-Cola, 2026-10-11, what WACC would you use?")
        to_gate = time.monotonic() - began
        began = time.monotonic()
        resume(graph, "latency", {"action": "confirm"})
        first = time.monotonic() - began
        follow = []
        for question in ("where does the revenue number come from?", "operating margin trend"):
            began = time.monotonic()
            graph.invoke({"message": question}, {"configurable": {"thread_id": "latency"}})
            follow.append(time.monotonic() - began)
        saver.conn.close()
        checkpoints.unlink(missing_ok=True)
        record("Chat: first message to Gate 0", [to_gate], "Router + checkpoint")
        record("Chat: confirm to WACC answer", [first], "data, WACC Agent, answer")
        record("Chat: follow-up question", follow, "intent rules, reuse data")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
