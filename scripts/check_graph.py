"""Run one real session through the graph, restart it from its checkpoint, finish it.

The first process stops at Gate 0; a second graph (new connection, same thread)
finds the waiting gate in the SQLite checkpoint, confirms it and runs the plan.
"""

import argparse
import time
import uuid
from datetime import date
from pathlib import Path

from dcf_agent.config import load_config
from dcf_agent.graph.services import LiveServices
from dcf_agent.graph.workflow import build_graph, open_checkpointer, pending_gate, resume, start

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", nargs="?", default="Coca-Cola, 2026-10-11, what WACC?")
    parser.add_argument("--today", type=date.fromisoformat, default=date(2026, 10, 15))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--live-model", action="store_true", help="Use Ollama for the Router")
    parser.add_argument("--checkpoints", type=Path, default=ROOT / "data/state/checkpoints.sqlite")
    args = parser.parse_args()
    config = load_config(ROOT)
    if args.live_model:
        config = config.model_copy(
            update={"settings": config.settings.model_copy(update={"mock_llm": False})}
        )
    session = "check-" + uuid.uuid4().hex[:8]
    with LiveServices(config, args.today, offline=args.offline) as live:
        saver = open_checkpointer(args.checkpoints)
        first = start(build_graph(live.services(), saver), session, args.message)
        gate = first["__interrupt__"][0].value
        print(f"Session {session} waiting at Gate 0:")
        print(f"  company {gate['company']}, date {gate['valuation_date']} ({gate['date_source']})")
        print(f"  intent {gate['intent']}, plan {gate['plan']}, scope {gate['scope']['status']}")
        for note in gate["notes"]:
            print("  note:", note)
        saver.conn.close()

        saver = open_checkpointer(args.checkpoints)
        graph = build_graph(live.services(), saver)
        assert pending_gate(graph, session) == gate, "restored gate differs"
        print("Restarted from the checkpoint; confirming.")
        began = time.monotonic()
        final = resume(graph, session, {"action": "confirm", "output": "question"})
        print(f"Status {final['status']} after {time.monotonic() - began:.1f}s")
        print(final["answer"])
        budget = final["budget"]
        print(f"Budget: {budget.steps_used} steps, {budget.wall_seconds_used:.1f}s")
        for event in final["audit_log"]:
            print(f"  {event.event_id} {event.component:6} {event.action:14} {event.rationale}")
        saver.conn.close()


if __name__ == "__main__":
    main()
