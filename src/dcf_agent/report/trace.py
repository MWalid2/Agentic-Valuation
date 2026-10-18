"""A session's trace as one static HTML page: steps, model calls, warnings, approvals.

Reads the session's checkpoint (its audit log and approvals) and the model traces
(`data/traces/llm.jsonl`), placing each model call in the session by its time.
"""

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from dcf_agent.contracts.workflow import ApprovalRecord, Event


def session_values(db: Path, session: str) -> dict[str, Any]:
    """The latest checkpoint's values of a session."""
    from dcf_agent.graph.workflow import open_checkpointer, thread

    saver = open_checkpointer(db)
    try:
        found = saver.get_tuple(thread(session))
    finally:
        saver.conn.close()
    if found is None:
        raise KeyError(f"no session {session} in {db}")
    return dict(found.checkpoint["channel_values"])


def sessions(db: Path) -> list[str]:
    import sqlite3

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "select thread_id, max(rowid) from checkpoints group by thread_id order by 2"
        ).fetchall()
    return [r[0] for r in rows]


def model_calls(traces: Path, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Model calls traced between `start` and `end` (only traces with a time)."""
    if not traces.exists():
        return []
    out = []
    for line in traces.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        at = entry.get("at")
        if at and start <= datetime.fromisoformat(at) <= end:
            out.append(entry)
    return out


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in r) + "</tr>" for r in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render(values: dict[str, Any], calls: Sequence[dict[str, Any]], title: str) -> str:
    events: list[Event] = list(values.get("audit_log", ()))
    approvals: list[ApprovalRecord] = list(values.get("approvals", ()))
    first = events[0].timestamp if events else None
    steps = []
    for e in events:
        offset = (e.timestamp - first).total_seconds() if first else 0.0
        steps.append(
            [
                f"{offset:,.0f}s",
                e.component,
                e.action,
                "" if e.duration_seconds is None else f"{e.duration_seconds:,.1f}s",
                (e.rationale or "")[:220],
            ]
        )
    per: dict[str, list[float]] = defaultdict(lambda: [0.0, 0, 0])
    for e in events:
        per[e.component][0] += e.duration_seconds or 0.0
        per[e.component][1] += 1
        per[e.component][2] += e.action in ("warning", "error")
    by_component = [
        [c, f"{v[0]:,.1f}s", int(v[1]), int(v[2])]
        for c, v in sorted(per.items(), key=lambda x: -x[1][0])
    ]
    roles: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0, 0, 0])
    for c in calls:
        r = roles[c["role"]]
        r[0] += 1
        r[1] += c.get("duration_seconds", 0.0)
        r[2] += c.get("prompt_tokens", 0) + c.get("output_tokens", 0)
        r[3] += c.get("attempt", 1) > 1
        r[4] += c.get("status") != "valid"
    by_role = [
        [k, int(v[0]), f"{v[1]:,.1f}s", int(v[2]), int(v[3]), int(v[4])]
        for k, v in sorted(roles.items(), key=lambda x: -x[1][1])
    ]
    gates = [
        [a.gate, a.decision, a.actor, "yes" if a.autopilot else "no", ", ".join(a.edit_refs)]
        for a in approvals
    ]
    critique = values.get("critique")
    valuation = values.get("valuation")
    budget = values.get("budget")
    total = budget.wall_seconds_used if budget is not None else sum(v[0] for v in per.values())
    summary = [
        f"Status: {values.get('status')}",
        f"Steps: {', '.join(values.get('done', ()))}",
        f"Run time: {total:,.0f}s across {len(events)} events",
    ]
    if valuation is not None:
        summary.append(f"Weighted value per share: {valuation.weighted:,.2f}")
    if critique is not None:
        summary.append(
            f"Critic: score {critique.score:.1f}, {len(critique.issues)} issues, "
            f"{critique.revision_count} loops"
        )
    if values.get("report_folder"):
        summary.append(f"Report: {values['report_folder']}")
    parts = [
        f"<h1>{escape(title)}</h1>",
        "<ul>" + "".join(f"<li>{escape(s)}</li>" for s in summary) + "</ul>",
        "<h2>Time by component</h2>",
        table(("Component", "Time", "Events", "Warnings"), by_component),
        "<h2>Model calls by role</h2>",
        table(("Role", "Calls", "Time", "Tokens", "Repairs", "Not valid"), by_role)
        if by_role
        else "<p>No timed model calls in this session (cached, or traced before times were "
        "recorded).</p>",
        "<h2>Approvals</h2>",
        table(("Gate", "Decision", "Actor", "Autopilot", "Edits"), gates),
    ]
    if critique is not None and critique.issues:
        parts += [
            "<h2>Critic issues</h2>",
            table(
                ("Severity", "Issue", "Owner"),
                [[i.severity, i.description, i.route_back_to] for i in critique.issues],
            ),
        ]
    parts += [
        "<h2>Timeline</h2>",
        table(("At", "Component", "Action", "Took", "What"), steps),
    ]
    style = (
        "body{font-family:Segoe UI,Arial,sans-serif;max-width:1100px;margin:2em auto;"
        "padding:0 1em;color:#222}table{border-collapse:collapse;margin:.5em 0 1.2em}"
        "td,th{border:1px solid #ccc;padding:3px 7px;text-align:left;vertical-align:top;"
        "font-size:13px}th{background:#ddebf7}"
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(title)}'
        f"</title><style>{style}</style></head><body>{''.join(parts)}</body></html>"
    )


def write_trace(db: Path, session: str, traces: Path, out: Path) -> Path:
    values = session_values(db, session)
    events = list(values.get("audit_log", ()))
    calls = model_calls(traces, events[0].timestamp, events[-1].timestamp) if events else []
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(values, calls, f"Trace: {session}"), encoding="utf-8")
    return out
