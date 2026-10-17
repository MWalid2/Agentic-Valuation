"""Phase 5 step 5 exit check: full runs with the report, for three golden companies.

Each company runs through the graph as the full model (I9): gates approved, comps,
the Critic (checks, rubric, loops), the workbook. Then:
- the workbook is recalculated by LibreOffice and compared with the engine (parity);
- its structure is checked (formula count, key cells, no hard-coded calculation cell);
- the report is written (memo in Markdown and HTML, football field, summary) to
  `outputs/reports/<ticker>-<date>/`, and the provenance audit is rerun.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from check_value import run

from dcf_agent.config import load_config
from dcf_agent.engine.audit import audit_state
from dcf_agent.engine.valuation import write_workbook
from dcf_agent.excel.checks import check_structure
from dcf_agent.excel.parity import ParityError, find_soffice, recalculate, verify
from dcf_agent.graph.services import LiveServices
from dcf_agent.graph.workflow import MARGINAL_TAX, build_graph, open_checkpointer
from dcf_agent.report.export import report_files
from dcf_agent.report.summary import headline

ROOT = Path(__file__).resolve().parents[1]


def workbook_checks(state: dict[str, Any], folder: Path) -> dict[str, Any]:
    valuation, scenarios = state["valuation"], state["forecast"]
    path, cells = write_workbook(
        state["data"],
        scenarios,
        state["wacc"],
        valuation,
        state.get("normalized"),
        MARGINAL_TAX,
        folder / "model.xlsx",
        comps=state.get("comps"),
        critique=state.get("critique"),
        critic_changes=state.get("critic_changes", ()),
    )
    structure = check_structure(
        path, cells, len(scenarios.scenarios), len(scenarios.scenarios[0].year_ends)
    )
    parity = "LibreOffice not found"
    if find_soffice() is not None:
        try:
            recalculated = recalculate(path, folder / "recalculated")
            checks = verify(
                recalculated,
                cells,
                value_per_share={r.scenario: r.value_per_share for r in valuation.results},
                enterprise_value={r.scenario: r.enterprise_value for r in valuation.results},
                weighted=valuation.weighted,
                wacc=state["wacc"].wacc.value,
            )
            failed = [c.label for c in checks if not c.passed]
            parity = f"{len(checks) - len(failed)}/{len(checks)}" + (
                f" failed {failed}" if failed else ""
            )
        except ParityError as error:
            parity = f"failed: {error}"
    return {
        "workbook": str(path),
        "parity": parity,
        "structure": structure.text(),
        "structure_passed": structure.passed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", default=["AAPL", "KO", "NKE"])
    parser.add_argument("--date", default="2026-10-11")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/report-check.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    config = load_config(ROOT)
    config = config.model_copy(
        update={"settings": config.settings.model_copy(update={"mock_llm": False})}
    )
    saver = open_checkpointer(ROOT / "outputs/report-check.sqlite")
    rows = []
    try:
        with LiveServices(config, date(2026, 10, 15), offline=True) as live:
            graph = build_graph(live.services(), saver)
            for ticker in args.tickers:
                began = time.monotonic()
                session = f"report-{ticker}-{int(time.time())}"
                state = run(graph, session, f"{ticker}, {args.date}", "excel")
                valuation = state.get("valuation")
                row: dict[str, Any] = {"ticker": ticker, "status": state.get("status")}
                if valuation is None:
                    row["error"] = [
                        e.rationale for e in state.get("audit_log", ()) if e.action == "warning"
                    ][-4:]
                    rows.append(row)
                    print(f"{ticker:5} not valued {row['error']}", flush=True)
                    continue
                folder = ROOT / f"outputs/reports/{ticker}-{valuation.as_of}"
                risks = [headline(text) for _, text in live.risks(state["data"])]
                summary, files = report_files(dict(state), folder, risk_factors=risks)
                critique = state.get("critique")
                row.update(
                    {
                        "steps": list(state.get("done", ())),
                        "exported": state.get("report_folder"),
                        "weighted": round(valuation.weighted, 2),
                        "range": [round(valuation.low, 2), round(valuation.high, 2)],
                        "price": valuation.price,
                        "comps": (
                            state["comps"].implied_value_range if state.get("comps") else None
                        ),
                        "critic": None
                        if critique is None
                        else {
                            "score": critique.score,
                            "passed": critique.passed,
                            "loops": critique.revision_count,
                            "rubric": critique.rubric_scores,
                            "issues": [f"{i.severity}: {i.description}" for i in critique.issues],
                        },
                        "drivers": [
                            f"{s.driver} {s.low:,.2f}-{s.high:,.2f}" for s in summary.drivers
                        ],
                        "risks": summary.risks,
                        **workbook_checks(dict(state), folder),
                        "files": {k: str(v) for k, v in files.items()},
                        "audit_problems": audit_state(dict(state)).problems[:3],
                        "seconds": round(time.monotonic() - began, 1),
                    }
                )
                rows.append(row)
                print(
                    f"{ticker:5} {row['weighted']:,.2f} ({row['range'][0]:,.2f}-"
                    f"{row['range'][1]:,.2f}) critic {row['critic'] and row['critic']['score']} "
                    f"parity {row['parity']} structure {row['structure_passed']} audit "
                    f"{'clean' if not row['audit_problems'] else row['audit_problems'][:1]} "
                    f"{row['seconds']}s",
                    flush=True,
                )
    finally:
        saver.conn.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2, default=str))
    print(
        json.dumps(
            {
                "companies": len(rows),
                "parity": sum(str(r.get("parity", "")).startswith("8/8") for r in rows),
                "structure": sum(bool(r.get("structure_passed")) for r in rows),
                "audit_clean": sum(not r.get("audit_problems", ["x"]) for r in rows),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
