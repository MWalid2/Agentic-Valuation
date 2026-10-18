"""Unattended batch runs: one full-model session per company, on autopilot.

Each company runs in its own checkpoint thread with a wall-clock budget. A crash is
caught and recorded, and the batch moves on. Results are appended to a JSON-lines
file, one record per company, so a stopped batch resumes where it left off.

Outcomes:
- success: exported with a value, a clean audit, workbook parity and structure, and
  no high-severity Critic issue;
- flagged: exported, the hard checks pass, but a high-severity Critic issue is left
  (listed in the report as unreviewed);
- failed: stopped with a classified reason the user can act on;
- wrong_number: exported while a hard check (audit, parity, structure) fails. This
  outcome must not happen.
"""

import json
import time
import traceback
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from dcf_agent.contracts.workflow import Budget
from dcf_agent.engine.audit import audit_state
from dcf_agent.graph.workflow import (
    MARGINAL_TAX,
    default_budget,
    pending_gate,
    resume,
    start,
)

Outcome = Literal["success", "flagged", "failed", "wrong_number"]
FailureClass = Literal[
    "resolution",  # Gate 0 could not resolve the company, the date or the scope
    "scope",  # out of scope (a financial company, no 10-K)
    "data",  # collection failed or statements are missing
    "normalization",
    "wacc",
    "forecast",
    "engine",  # an engine guardrail refused the scenarios (e.g. WACC at or below growth)
    "comps",
    "export",
    "time_limit",
    "crash",
    "critic",  # exported with a high-severity Critic issue left
    "checks",  # exported with a failed hard check
]
# What to do about each failure, for the record and the user.
ADVICE: dict[str, str] = {
    "resolution": "check the ticker and the date, or choose among the candidates",
    "scope": "the company is outside v1 (financials, REITs, no 10-K)",
    "data": "the filings lack a statement the model needs; see the data warnings",
    "normalization": "the history could not be normalized (for example no revenue: a "
    "pre-revenue company is outside v1); see the normalization warnings",
    "wacc": "check the industry and the market data; an industry override may help",
    "forecast": "the forecast could not be built; see its notes",
    "engine": "the scenarios break an engine guardrail; edit them at Gate 2",
    "comps": "comps are a cross-check; the value stands without them",
    "export": "the report could not be written; check the outputs folder",
    "time_limit": "the run exceeded its time limit; rerun it alone or raise the limit",
    "crash": "an unexpected error; see the exception",
    "critic": "review the Critic's high-severity issue before relying on the value",
    "checks": "a hard check failed: do not use this value",
}


@dataclass
class CompanyResult:
    ticker: str
    as_of: str
    outcome: Outcome
    failure: str | None = None
    reason: str = ""
    advice: str = ""
    status: str | None = None
    steps: list[str] = field(default_factory=list)
    value: float | None = None
    low: float | None = None
    high: float | None = None
    price: float | None = None
    critic_score: float | None = None
    critic_high: list[str] = field(default_factory=list)
    critic_loops: int | None = None
    peers: list[str] = field(default_factory=list)
    audit_clean: bool | None = None
    audit_problems: list[str] = field(default_factory=list)
    structure: bool | None = None
    parity: str | None = None
    report: str | None = None
    seconds: float = 0.0
    step_seconds: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def warnings_of(state: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (e.component, e.rationale or "")
        for e in state.get("audit_log", ())
        if e.action in ("warning", "error")
    ]


def step_seconds(state: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for e in state.get("audit_log", ()):
        if e.duration_seconds:
            out[e.component] += e.duration_seconds
    return {k: round(v, 1) for k, v in out.items()}


def failure_of(state: dict[str, Any]) -> tuple[str, str]:
    """The failure class and reason of a session that did not export."""
    status = state.get("status")
    found = warnings_of(state)
    if status == "budget_exhausted":
        return "time_limit", "the run budget was exhausted"
    if state.get("request") is None:
        route = state.get("route")
        reason = "; ".join(t for c, t in found if c == "router") or "company not resolved"
        if route is not None and getattr(route, "scope", None) is not None:
            scope = route.scope
            if getattr(scope, "status", None) == "blocked":
                return "scope", "; ".join(scope.missing.get("scope", ())) or "out of scope"
        return "resolution", reason
    if status == "failed" or state.get("data") is None:
        return "data", state.get("answer") or "; ".join(t for c, t in found if c == "data")
    by = {
        "normalizer": "normalization",
        "wacc": "wacc",
        "forecast": "forecast",
        "engine": "engine",
        "comps": "comps",
        "system": "export",
    }
    if state.get("forecast") is None and state.get("normalized") is None:
        normalizer = [t for c, t in found if c == "normalizer" and "failed" in t]
        if normalizer:
            return "normalization", "; ".join(normalizer)
    if state.get("wacc") is None:
        reason = "; ".join(t for c, t in found if c == "wacc") or "no WACC"
        # The data step's own reason, when a WACC input (market, macro, betas) is missing.
        bundle = state.get("data")
        failed = [
            f"{c.field}: {c.message}"
            for c in (bundle.quality.checks if bundle is not None else ())
            if c.status == "failed"
            and c.severity == "error"
            and c.field in ("market", "macro", "industry")
        ]
        return "wacc", "; ".join([reason, *failed[:2]])
    if state.get("forecast") is None:
        return "forecast", "; ".join(t for c, t in found if c == "forecast") or "no forecast"
    if state.get("valuation") is None:
        return "engine", "; ".join(t for c, t in found if c == "engine") or "not valued"
    if not state.get("report_folder"):
        return "export", "; ".join(t for c, t in found if c == "system") or "no report"
    component = found[-1][0] if found else "system"
    return by.get(component, "crash"), found[-1][1] if found else "stopped"


def verify_workbook(state: dict[str, Any], folder: Path) -> tuple[bool, str]:
    """Structure and LibreOffice parity of the session's workbook, rebuilt in `folder`."""
    from dcf_agent.engine.valuation import write_workbook
    from dcf_agent.excel.checks import check_structure
    from dcf_agent.excel.parity import ParityError, find_soffice, recalculate, verify

    valuation, scenarios = state["valuation"], state["forecast"]
    path, cells = write_workbook(
        state["data"],
        scenarios,
        state["wacc"],
        valuation,
        state.get("normalized"),
        MARGINAL_TAX,
        folder / "verify.xlsx",
        comps=state.get("comps"),
        critique=state.get("critique"),
        critic_changes=state.get("critic_changes", ()),
    )
    first = scenarios.scenarios[0]
    structure = check_structure(path, cells, len(scenarios.scenarios), len(first.year_ends))
    if find_soffice() is None:
        return structure.passed, "LibreOffice not found"
    try:
        recalculated = recalculate(path, folder / "recalculated")
    except ParityError as error:
        return structure.passed, f"failed: {error}"
    checks = verify(
        recalculated,
        cells,
        value_per_share={r.scenario: r.value_per_share for r in valuation.results},
        enterprise_value={r.scenario: r.enterprise_value for r in valuation.results},
        weighted=valuation.weighted,
        wacc=state["wacc"].wacc.value,
    )
    failed = [c.label for c in checks if not c.passed]
    return structure.passed, f"{len(checks) - len(failed)}/{len(checks)}" + (
        f" failed {failed}" if failed else ""
    )


def run_session(
    graph: Any, session: str, ticker: str, as_of: date, budget: Budget
) -> dict[str, Any]:
    """A full-model session on autopilot; Gate 0 is confirmed once, the rest approve."""
    state = start(graph, session, f"{ticker}, {as_of}", budget)
    gate = pending_gate(graph, session)
    if gate is None or gate["gate"] != 0:
        return state
    if not gate.get("ready", True):
        return state  # unresolved: classified from the route
    state = resume(graph, session, {"action": "confirm", "output": "excel", "autopilot": True})
    if pending_gate(graph, session) is not None:  # Gate 0 again: not confirmed
        return state
    return state


def run_company(
    graph: Any,
    ticker: str,
    as_of: date,
    *,
    budget: Budget | None = None,
    parity: bool = True,
    session: str | None = None,
) -> CompanyResult:
    began = time.monotonic()
    session = session or f"batch-{ticker}-{as_of}-{int(time.time())}"
    try:
        state = run_session(graph, session, ticker, as_of, budget or default_budget())
    except Exception as error:  # one company's crash never stops the batch
        return CompanyResult(
            ticker=ticker,
            as_of=str(as_of),
            outcome="failed",
            failure="crash",
            reason=f"{type(error).__name__}: {error}"[:500],
            advice=ADVICE["crash"],
            warnings=traceback.format_exc().splitlines()[-6:],
            seconds=round(time.monotonic() - began, 1),
        )
    valuation, critique, comps = state.get("valuation"), state.get("critique"), state.get("comps")
    result = CompanyResult(
        ticker=ticker,
        as_of=str(as_of),
        outcome="failed",
        status=state.get("status"),
        steps=list(state.get("done", ())),
        value=valuation.weighted if valuation else None,
        low=valuation.low if valuation else None,
        high=valuation.high if valuation else None,
        price=valuation.price if valuation else None,
        critic_score=critique.score if critique else None,
        critic_high=[i.description for i in critique.issues if i.severity == "high"]
        if critique
        else [],
        critic_loops=critique.revision_count if critique else None,
        peers=[p.ticker for p in comps.peers] if comps else [],
        report=state.get("report_folder"),
        step_seconds=step_seconds(state),
        warnings=[f"{c}: {t[:200]}" for c, t in warnings_of(state)][-10:],
    )
    if state.get("data") is not None:
        audit = audit_state(state)
        result.audit_clean, result.audit_problems = audit.clean, audit.problems[:5]
    if not result.report or valuation is None:
        result.failure, result.reason = failure_of(state)
        result.reason = result.reason[:500]
        result.advice = ADVICE.get(result.failure, "")
        result.seconds = round(time.monotonic() - began, 1)
        return result
    structure, parity_text = True, None
    if parity:
        structure, parity_text = verify_workbook(state, Path(result.report))
    result.structure, result.parity = structure, parity_text
    parity_ok = parity_text is None or parity_text.startswith(("8/8", "LibreOffice not"))
    if not (result.audit_clean and structure and parity_ok):
        result.outcome, result.failure = "wrong_number", "checks"
        result.reason = (
            f"audit {'clean' if result.audit_clean else 'not clean'}; structure "
            f"{structure}; parity {parity_text}"
        )
    elif result.critic_high:
        result.outcome, result.failure = "flagged", "critic"
        result.reason = "; ".join(result.critic_high)[:500]
    else:
        result.outcome = "success"
    result.advice = ADVICE.get(result.failure or "", "")
    result.seconds = round(time.monotonic() - began, 1)
    return result


def done_tickers(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        json.loads(line)["ticker"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def run_batch(
    graph: Any,
    tickers: Iterable[str],
    as_of: date,
    out: Path,
    *,
    budget: Budget | None = None,
    parity: bool = True,
    on_result: Callable[[CompanyResult], None] | None = None,
) -> list[CompanyResult]:
    """Every company not already in `out`, one record appended after each."""
    out.parent.mkdir(parents=True, exist_ok=True)
    finished = done_tickers(out)
    results = []
    for ticker in tickers:
        if ticker in finished:
            continue
        result = run_company(graph, ticker, as_of, budget=budget, parity=parity)
        with out.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(result), default=str) + "\n")
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


def summarize(path: Path) -> dict[str, Any]:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    outcomes: dict[str, int] = defaultdict(int)
    failures: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        outcomes[r["outcome"]] += 1
        if r["failure"]:
            failures[r["failure"]].append(r["ticker"])
    return {
        "companies": len(rows),
        "outcomes": dict(outcomes),
        "failures": dict(failures),
        "success_rate": round(outcomes["success"] / len(rows), 3) if rows else None,
        "seconds": sorted(r["seconds"] for r in rows),
    }
