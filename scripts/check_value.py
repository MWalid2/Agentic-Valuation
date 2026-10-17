"""Phase 4 step 4 exit check: quick value end to end through the graph, offline.

For each golden company the graph runs as a session would: Router, Gate 0 (confirm,
quick value), data, normalization with Gate 1 (all proposals approved), WACC,
forecast, valuation, answer. The answer must give a cited value per share. One
company also runs the full model (I9), whose workbook is recalculated by LibreOffice
and checked against the engine (parity), as in Phase 1.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from dcf_agent.config import load_config
from dcf_agent.engine.audit import audit_state
from dcf_agent.graph.services import LiveServices
from dcf_agent.graph.workflow import build_graph, open_checkpointer, pending_gate, resume, start

ROOT = Path(__file__).resolve().parents[1]
COMPANIES = ("AAPL", "BA", "CAT", "DE", "KO", "MCD", "MSFT", "NKE", "NVDA", "SNOW")


def run(graph: Any, session: str, message: str, output: str) -> dict[str, Any]:
    state = start(graph, session, message)
    for _ in range(8):
        gate = pending_gate(graph, session)
        if gate is None:
            break
        if gate["gate"] == 0:
            state = resume(graph, session, {"action": "confirm", "output": output})
        elif gate.get("kind") == "adjustments":
            state = resume(graph, session, {"action": "approve_all"})
        elif gate.get("kind") == "assumptions":
            state = resume(graph, session, {"action": "approve"})
        elif gate.get("kind") == "signoff":
            state = resume(graph, session, {"action": "approve"})
        else:
            state = resume(graph, session, {"action": "continue"})
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", default=list(COMPANIES))
    parser.add_argument("--date", default="2026-10-11")
    parser.add_argument("--full", default="KO", help="Company also run as the full model (I9)")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/value-check.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    config = load_config(ROOT)
    config = config.model_copy(
        update={"settings": config.settings.model_copy(update={"mock_llm": False})}
    )
    saver = open_checkpointer(ROOT / "outputs/value-check.sqlite")
    rows = []
    states: dict[str, dict[str, Any]] = {}
    try:
        with LiveServices(config, date(2026, 10, 15), offline=True) as live:
            graph = build_graph(live.services(), saver)
            jobs = [(t, "quick") for t in args.tickers] + (
                [(args.full, "excel")] if args.full else []
            )
            for ticker, output in jobs:
                began = time.monotonic()
                session = f"value-{ticker}-{output}-{int(time.time())}"
                state = run(graph, session, f"{ticker}, {args.date}", output)
                valuation = state.get("valuation")
                row = {
                    "ticker": ticker,
                    "output": output,
                    "status": state.get("status"),
                    "steps": list(state.get("done", ())),
                    "weighted": valuation.weighted if valuation else None,
                    "range": [valuation.low, valuation.high] if valuation else None,
                    "price": valuation.price if valuation else None,
                    "workbook": valuation.workbook if valuation else None,
                    "cited": "Value per share" in (state.get("answer") or "")
                    and "[1]" in (state.get("answer") or ""),
                    "seconds": round(time.monotonic() - began, 1),
                    "audit": (lambda a: {"records": a.records, "problems": a.problems[:5]})(
                        audit_state(state)
                    ),
                    "warnings": [
                        e.rationale for e in state.get("audit_log", ()) if e.action == "warning"
                    ][-6:],
                }
                rows.append(row)
                if output == "excel" and valuation is not None:
                    states[ticker] = state
                shown = (
                    f"{row['weighted']:,.2f} ({row['range'][0]:,.2f}-{row['range'][1]:,.2f})"
                    if valuation
                    else "not valued"
                )
                print(
                    f"{ticker:5} {output:5} {row['status']:8} {shown:28} price "
                    f"{row['price'] if row['price'] is None else f'{row["price"]:,.2f}'} cited "
                    f"{row['cited']} {row['seconds']}s",
                    flush=True,
                )
                if not valuation:
                    for warning in row["warnings"]:
                        print(f"      {warning[:160]}")
    finally:
        saver.conn.close()
    parity = None
    full = next((r for r in rows if r["output"] == "excel" and r["workbook"]), None)
    if full is not None:
        parity = check_parity(states[full["ticker"]], Path(full["workbook"]))
        print(f"Workbook {full['workbook']}: parity {parity}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "companies": len([r for r in rows if r["output"] == "quick"]),
        "valued": sum(bool(r["weighted"] is not None) for r in rows if r["output"] == "quick"),
        "cited": sum(r["cited"] for r in rows if r["output"] == "quick"),
        "audit_clean": sum(not r["audit"]["problems"] for r in rows if r["output"] == "quick"),
        "parity": parity,
    }
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))
    print(json.dumps(summary, indent=2))


def check_parity(state: dict[str, Any], path: Path) -> str:
    """Recalculate the workbook in LibreOffice and compare it with the engine (Phase 1)."""
    from dcf_agent.engine.valuation import write_workbook
    from dcf_agent.excel.parity import ParityError, find_soffice, recalculate, verify
    from dcf_agent.graph.workflow import MARGINAL_TAX

    if find_soffice() is None:
        return "LibreOffice not found"
    valuation = state["valuation"]
    path, cells = write_workbook(
        state["data"],
        state["forecast"],
        state["wacc"],
        valuation,
        state.get("normalized"),
        MARGINAL_TAX,
        path,
    )
    try:
        recalculated = recalculate(path, path.parent / "recalculated")
    except ParityError as error:
        return f"failed: {error}"
    checks = verify(
        recalculated,
        cells,
        value_per_share={r.scenario: r.value_per_share for r in valuation.results},
        enterprise_value={r.scenario: r.enterprise_value for r in valuation.results},
        weighted=valuation.weighted,
        wacc=state["wacc"].wacc.value,
    )
    failed = [c for c in checks if not c.passed]
    return f"{len(checks) - len(failed)}/{len(checks)} checks pass" + (
        f"; failed: {[c.label for c in failed]}" if failed else ""
    )


if __name__ == "__main__":
    main()
