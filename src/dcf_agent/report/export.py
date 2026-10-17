"""Write the report: memo (Markdown, HTML), football-field chart, summary JSON."""

import json
import shutil
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dcf_agent.engine.audit import audit_state
from dcf_agent.report.chart import football_field
from dcf_agent.report.memo import html, markdown, sections
from dcf_agent.report.summary import Summary, build_summary

CHART = "football-field.svg"


def report_files(
    state: dict[str, Any],
    folder: Path,
    *,
    risk_factors: Sequence[str] = (),
    unreviewed: Sequence[str] = (),
) -> tuple[Summary, dict[str, Path]]:
    """The summary and the files written in `folder` (memo.md, memo.html, chart, summary)."""
    summary = build_summary(state, risk_factors)
    chart = football_field(
        summary.ranges, summary.price, f"{summary.ticker}: value per share (USD)"
    )
    parts = sections(state, summary, chart=chart, unreviewed=unreviewed)
    folder.mkdir(parents=True, exist_ok=True)
    files = {
        "chart": folder / CHART,
        "memo_md": folder / "memo.md",
        "memo_html": folder / "memo.html",
        "summary": folder / "summary.json",
    }
    files["chart"].write_text(chart, encoding="utf-8")
    files["memo_md"].write_text(markdown(summary, parts, CHART), encoding="utf-8")
    files["memo_html"].write_text(html(summary, parts), encoding="utf-8")
    data = asdict(summary)
    data["critic"] = summary.critic.model_dump(mode="json") if summary.critic else None
    data["unreviewed"] = list(unreviewed)
    files["summary"].write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return summary, files


def signoff_payload(summary: Summary, state: dict[str, Any]) -> dict[str, Any]:
    """What Gate 3 shows, as plain data: values, weights, ranges, drivers, the Critic."""
    critic = summary.critic
    return {
        "gate": 3,
        "kind": "signoff",
        "ticker": summary.ticker,
        "company": summary.company,
        "as_of": str(summary.as_of),
        "weighted": summary.weighted,
        "low": summary.low,
        "high": summary.high,
        "price": summary.price,
        "gap": summary.gap,
        "scenarios": [
            {"scenario": name, "probability": p, "value_per_share": v}
            for name, (p, v) in summary.scenarios.items()
        ],
        "ranges": [
            {"label": r.label, "low": r.low, "high": r.high, "mark": r.mark} for r in summary.ranges
        ],
        "drivers": [
            {"driver": s.driver, "step": s.step, "low": s.low, "high": s.high}
            for s in summary.drivers
        ],
        "waccs": list(summary.waccs),
        "growths": list(summary.growths),
        "grid": [list(row) for row in summary.grid],
        "critic": None
        if critic is None
        else {
            "score": critic.score,
            "passed": critic.passed,
            "loops": critic.revision_count,
            "rubric": critic.rubric_scores,
            "issues": [
                {
                    "severity": i.severity,
                    "description": i.description,
                    "owner": i.route_back_to,
                    "fix": i.suggested_fix,
                }
                for i in critic.issues
            ],
        },
        "changes": list(state.get("critic_changes", ())),
        "risks": summary.risks,
        "unreviewed": list(state.get("unreviewed", ())),
        "actions": ["approve", "back", "stop"],
    }


def export(
    state: dict[str, Any],
    folder: Path,
    risk_factors: Sequence[str] = (),
    unreviewed: Sequence[str] = (),
) -> dict[str, Path]:
    """The approved report: memo, chart, summary, workbook copy, audit log, provenance audit."""
    _, files = report_files(state, folder, risk_factors=risk_factors, unreviewed=unreviewed)
    valuation = state["valuation"]
    if valuation.workbook and Path(valuation.workbook).exists():
        files["workbook"] = folder / "model.xlsx"
        shutil.copyfile(valuation.workbook, files["workbook"])
    files["audit_log"] = folder / "audit-log.json"
    files["audit_log"].write_text(
        json.dumps(
            {
                "events": [e.model_dump(mode="json") for e in state.get("audit_log", ())],
                "approvals": [a.model_dump(mode="json") for a in state.get("approvals", ())],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    audit = audit_state(state)
    files["provenance"] = folder / "provenance-audit.json"
    files["provenance"].write_text(
        json.dumps(
            {
                "records": audit.records,
                "by_source": dict(audit.by_source),
                "clean": audit.clean,
                "problems": audit.problems,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return files
