"""Phase 6: unattended full-model runs on a cohort (autopilot), resumable.

Each company runs as a full-model session on autopilot with a wall-clock budget; the
result (outcome, failure class and reason, value, Critic, comps, audit, workbook
parity and structure, time per step, report folder) is appended to the results file.
Rerunning skips companies already recorded, so a cohort can run in chunks:

    uv run python scripts/run_batch.py scripts/cohorts/golden20.json --chunk 5

Online the first time for a new company (its SEC data and its peers'), then cached.
"""

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

from dcf_agent.batch import ADVICE, CompanyResult, done_tickers, run_batch, summarize
from dcf_agent.config import load_config
from dcf_agent.contracts.workflow import Budget
from dcf_agent.graph.services import LiveServices
from dcf_agent.graph.workflow import build_graph, open_checkpointer

ROOT = Path(__file__).resolve().parents[1]


def show(r: CompanyResult) -> None:
    value = "-" if r.value is None else f"{r.value:,.2f}"
    price = "-" if r.price is None else f"{r.price:,.2f}"
    print(
        f"{r.ticker:6} {r.outcome:12} {r.failure or '':13} value {value:>10} price {price:>10} "
        f"critic {r.critic_score} peers {len(r.peers)} parity {r.parity} {r.seconds:.0f}s",
        flush=True,
    )
    if r.reason:
        print(f"       {r.reason[:200]}", flush=True)


def isolate(args: argparse.Namespace, pending: list[str], out: Path) -> None:
    """Each company in its own process, killed at the time limit (a hung step included)."""
    limit = args.minutes * 60 + 300  # the in-process budget first; the kill as a backstop
    flags = [str(args.cohort), "--date", str(args.date), "--out", str(out)]
    flags += ["--minutes", str(args.minutes)]
    flags += [
        f
        for f, on in (
            ("--no-parity", args.no_parity),
            ("--offline", args.offline),
            ("--no-cache", args.no_cache),
        )
        if on
    ]
    for ticker in pending:
        began = time.monotonic()
        try:
            done = subprocess.run(
                [sys.executable, "-u", __file__, *flags, "--only", ticker],
                timeout=limit,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in done.stdout.splitlines():
                if line.startswith(ticker):
                    print(line, flush=True)
            failure, reason = "crash", (done.stderr or done.stdout)[-400:]
            if done.returncode == 0:
                continue
        except subprocess.TimeoutExpired:
            failure, reason = "time_limit", f"killed after {limit / 60:.0f} minutes"
        if ticker in done_tickers(out):
            continue
        result = CompanyResult(
            ticker=ticker,
            as_of=str(args.date),
            outcome="failed",
            failure=failure,
            reason=reason.strip(),
            advice=ADVICE[failure],
            seconds=round(time.monotonic() - began, 1),
        )
        with out.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(result), default=str) + "\n")
        show(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", type=Path)
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--chunk", type=int, default=0, help="at most this many new companies")
    parser.add_argument("--minutes", type=float, default=45.0, help="time limit per company")
    parser.add_argument("--no-parity", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--no-cache", action="store_true", help="every model call runs (cold)")
    parser.add_argument("--only", help=argparse.SUPPRESS)  # the child process: one company
    parser.add_argument(
        "--in-process", action="store_true", help="no child process per company (no hard kill)"
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    out = args.out or ROOT / f"outputs/batch/{args.cohort.stem}-{args.date}.jsonl"
    tickers = [e["ticker"] for e in json.loads(args.cohort.read_text(encoding="utf-8"))]
    pending = [t for t in tickers if t not in done_tickers(out)]
    if args.chunk:
        pending = pending[: args.chunk]
    if args.only:
        pending = [args.only]
    elif not args.in_process:
        isolate(args, pending, out)
        print(json.dumps(summarize(out), indent=2))
        return
    config = load_config(ROOT)
    config = config.model_copy(
        update={"settings": config.settings.model_copy(update={"mock_llm": False})}
    )
    if args.no_cache:
        runtime = config.models.runtime.model_copy(update={"response_cache": False})
        config = config.model_copy(
            update={"models": config.models.model_copy(update={"runtime": runtime})}
        )
    budget = Budget(max_steps=40, max_tokens=500_000, max_wall_seconds=args.minutes * 60)
    saver = open_checkpointer(ROOT / "outputs/batch/sessions.sqlite")
    try:
        with LiveServices(config, date.today(), offline=args.offline) as live:
            graph = build_graph(live.services(), saver)
            run_batch(
                graph,
                pending,
                args.date,
                out,
                budget=budget,
                parity=not args.no_parity,
                on_result=show,
            )
    finally:
        saver.conn.close()
    print(json.dumps(summarize(out), indent=2))


if __name__ == "__main__":
    main()
