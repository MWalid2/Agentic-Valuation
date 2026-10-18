"""Write a session's trace view (static HTML): steps, model calls, warnings, approvals.

uv run python scripts/trace_report.py outputs/batch/sessions.sqlite --session <id>
uv run python scripts/trace_report.py outputs/batch/sessions.sqlite --latest
"""

import argparse
from pathlib import Path

from dcf_agent.report.trace import sessions, write_trace

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path)
    parser.add_argument("--session")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--traces", type=Path, default=ROOT / "data/traces/llm.jsonl")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    known = sessions(args.db)
    session = args.session or (known[-1] if args.latest and known else None)
    if session is None:
        print("Sessions:", *known[-20:], sep="\n  ")
        return
    out = args.out or ROOT / f"outputs/traces/{session}.html"
    print(write_trace(args.db, session, args.traces, out))


if __name__ == "__main__":
    main()
