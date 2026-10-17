"""The valuation memo: one structure rendered as Markdown and as HTML.

Every number is injected from the engine or the state. The scenario stories are the
Forecast Agent's (approved at Gate 2) and are shown as such; nothing else here is
written by a model.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape
from typing import Any

from dcf_agent.contracts.modeling import CompsResult, ForecastScenarios, WACCResult
from dcf_agent.contracts.workflow import CritiqueReport
from dcf_agent.engine.audit import audit_state
from dcf_agent.report.summary import Summary

DISCLAIMER = (
    "Educational model, not investment advice. Every input is sourced in the workbook's "
    "Sources sheet and the audit log; review them before relying on the value."
)


@dataclass
class Table:
    headers: Sequence[str]
    rows: Sequence[Sequence[str]]


@dataclass
class Section:
    title: str
    blocks: list[str | Table | list[str]] = field(default_factory=list)  # text, table, list


def usd(v: float | None) -> str:
    return "-" if v is None else f"${v:,.2f}"


def pct(v: float | None, digits: int = 1) -> str:
    return "-" if v is None else f"{v:.{digits}%}"


def big(v: float) -> str:
    return f"${v / 1e9:,.1f}B" if abs(v) >= 1e8 else f"${v:,.0f}"


def sections(
    state: dict[str, Any],
    summary: Summary,
    *,
    chart: str | None = None,
    unreviewed: Sequence[str] = (),
) -> list[Section]:
    scenarios: ForecastScenarios = state["forecast"]
    wacc: WACCResult = state["wacc"]
    comps: CompsResult | None = state.get("comps")
    critic: CritiqueReport | None = summary.critic
    out: list[Section] = []

    gap = summary.gap
    head = Section("Summary")
    head.blocks.append(
        f"Probability-weighted value **{usd(summary.weighted)}** per share "
        f"(bear {usd(summary.low)} to bull {usd(summary.high)}; base {usd(summary.base)}), "
        + (
            f"against a price of {usd(summary.price)} on {summary.price_on} ({gap:+.0%})."
            if gap is not None
            else "no market price."
        )
    )
    if comps is not None and comps.implied_value_range is not None:
        low, high = comps.implied_value_range
        head.blocks.append(
            f"Comps imply {usd(low)} to {usd(high)} from {len(comps.peers)} peers' median "
            "multiples (a cross-check, not blended into the value)."
        )
    if critic is not None:
        state_word = "passed" if critic.passed else "did not pass"
        head.blocks.append(
            f"The Critic's review {state_word}: score {critic.score:.1f} of 5, "
            f"{len(critic.issues)} issues, {critic.revision_count} revision loops."
        )
    head.blocks.append(
        [
            f"{s.driver} ({s.step}): {usd(min(s.low, s.high))} to {usd(max(s.low, s.high))}"
            for s in summary.drivers
        ]
    )
    out.append(head)
    if chart:
        out.append(Section("Football field", [chart]))

    rows = []
    stories: list[str] = []
    for item in scenarios.scenarios:
        probability, value = summary.scenarios[item.scenario]
        rows.append(
            [
                item.scenario,
                pct(probability, 0),
                usd(value),
                pct(item.revenue_growth[0].value),
                pct(item.operating_margin[min(4, len(item.operating_margin) - 1)].value),
                pct(item.terminal_growth.value),
                pct(item.terminal_roic.value),
            ]
        )
        stories.append(f"**{item.scenario.capitalize()}.** {item.narrative}")
    out.append(
        Section(
            "Scenarios",
            [
                Table(
                    (
                        "Scenario",
                        "Probability",
                        "Value per share",
                        "Growth, year 1",
                        "Margin, year 5",
                        "Terminal growth",
                        "Terminal ROIC",
                    ),
                    rows,
                ),
                "Stories by the Forecast Agent, approved at Gate 2:",
                *stories,
            ],
        )
    )
    out.append(
        Section(
            "What drives the value",
            [
                "Base scenario, one driver moved at a time (engine):",
                Table(
                    ("Driver", "Move", "Down", "Up"),
                    [[s.driver, s.step, usd(s.low), usd(s.high)] for s in summary.drivers],
                ),
            ],
        )
    )
    if summary.grid:
        out.append(
            Section(
                "Sensitivity: WACC x terminal growth (base, value per share)",
                [
                    Table(
                        ("WACC \\ g", *(pct(g, 2) for g in summary.growths)),
                        [
                            [pct(w, 2), *(usd(v) for v in row)]
                            for w, row in zip(summary.waccs, summary.grid, strict=True)
                        ],
                    )
                ],
            )
        )
    out.append(
        Section(
            "Cost of capital",
            [
                Table(
                    ("Item", "Value", "Source"),
                    [
                        ["Risk-free rate", pct(wacc.rf.value, 2), wacc.rf.prov.ref[:90]],
                        ["Equity risk premium", pct(wacc.erp.value, 2), wacc.erp.prov.ref[:90]],
                        ["Beta (relevered)", f"{wacc.relevered_beta.value:.2f}", wacc.beta_method],
                        ["Cost of equity", pct(wacc.cost_equity.value, 2), "engine"],
                        ["After-tax cost of debt", pct(wacc.aftertax_kd.value, 2), "engine"],
                        ["Weight of debt", pct(wacc.weight_debt.value, 1), "engine"],
                        ["WACC", pct(wacc.wacc.value, 2), "engine"],
                        ["Terminal WACC", pct(wacc.terminal_wacc.value, 2), "engine"],
                    ],
                )
            ],
        )
    )
    if comps is not None:
        block: list[str | Table | list[str]] = [
            Table(
                ("Multiple", "Peers", "Q1", "Median", "Q3"),
                [
                    [
                        s.metric.replace("_", "/").upper(),
                        str(s.sample_size),
                        f"{s.first_quartile:.1f}x",
                        f"{s.median:.1f}x",
                        f"{s.third_quartile:.1f}x",
                    ]
                    for s in comps.summaries
                ],
            ),
            "Peers: " + ", ".join(p.ticker for p in comps.peers) + ".",
        ]
        if comps.warnings:
            block.append(list(comps.warnings))
        out.append(Section("Comparable companies", block))
    adjustments = state.get("approved_adjustments", ())
    if adjustments:
        out.append(
            Section(
                "Normalization (approved at Gate 1)",
                [
                    Table(
                        ("Item", "Year", "Amount", "Effect"),
                        [
                            [a.item, str(a.period_end.year), big(a.amount.value), a.direction]
                            for a in adjustments
                        ],
                    )
                ],
            )
        )
    review: list[str | Table | list[str]] = []
    if critic is not None and critic.issues:
        review.append(
            Table(
                ("Severity", "Issue", "Owner", "Suggested fix"),
                [
                    [i.severity, i.description, i.route_back_to, i.suggested_fix]
                    for i in critic.issues
                ],
            )
        )
    changes = state.get("critic_changes", ())
    if changes:
        review += ["Changes made by the Critic's loops:", list(changes)]
    if summary.risks:
        review += ["Main risks:", list(summary.risks)]
    if review:
        out.append(Section("Review and risks", review))
    if unreviewed:
        out.append(Section("Unreviewed items (autopilot)", [list(unreviewed)]))
    audit = audit_state(state)
    out.append(
        Section(
            "Sources",
            [
                f"{audit.records:,} sourced values checked by the provenance audit: "
                + ("all traceable." if audit.clean else f"{len(audit.problems)} problems.")
                + " By source: "
                + ", ".join(f"{k} {v:,}" for k, v in audit.by_source.most_common())
                + ".",
                DISCLAIMER,
            ],
        )
    )
    return out


def title(summary: Summary) -> str:
    return f"{summary.company} ({summary.ticker}): DCF valuation as of {summary.as_of}"


def markdown(summary: Summary, parts: Sequence[Section], chart_file: str | None = None) -> str:
    lines = [f"# {title(summary)}", ""]
    for section in parts:
        lines += [f"## {section.title}", ""]
        for block in section.blocks:
            if isinstance(block, Table):
                lines.append("| " + " | ".join(block.headers) + " |")
                lines.append("|" + "---|" * len(block.headers))
                lines += [
                    "| " + " | ".join(c.replace("|", "/") for c in r) + " |" for r in block.rows
                ]
            elif isinstance(block, list):
                lines += [f"- {item}" for item in block]
            elif block.lstrip().startswith("<svg"):
                lines.append(f"![Football field]({chart_file})" if chart_file else "")
            else:
                lines.append(block)
            lines.append("")
    return "\n".join(lines)


def inline(text: str) -> str:
    """Escaped text with **bold** kept."""
    parts = escape(text).split("**")
    return "".join(f"<strong>{p}</strong>" if k % 2 else p for k, p in enumerate(parts))


def html(summary: Summary, parts: Sequence[Section]) -> str:
    body = [f"<h1>{escape(title(summary))}</h1>"]
    for section in parts:
        body.append(f"<h2>{escape(section.title)}</h2>")
        for block in section.blocks:
            if isinstance(block, Table):
                head = "".join(f"<th>{escape(h)}</th>" for h in block.headers)
                rows = "".join(
                    "<tr>" + "".join(f"<td>{escape(c)}</td>" for c in r) + "</tr>"
                    for r in block.rows
                )
                body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
            elif isinstance(block, list):
                body.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in block) + "</ul>")
            elif block.lstrip().startswith("<svg"):
                body.append(f'<div class="chart">{block}</div>')  # drawn by our own code
            else:
                body.append(f"<p>{inline(block)}</p>")
    style = (
        "body{font-family:Segoe UI,Arial,sans-serif;max-width:960px;margin:2em auto;"
        "padding:0 1em;color:#222;line-height:1.45}table{border-collapse:collapse;"
        "margin:.5em 0 1em}td,th{border:1px solid #ccc;padding:4px 8px;text-align:left;"
        "vertical-align:top}th{background:#ddebf7}h2{border-bottom:1px solid #ddd;"
        "margin-top:1.6em}.chart{overflow-x:auto}"
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{escape(title(summary))}</title><style>{style}</style></head>"
        f"<body>{''.join(body)}</body></html>"
    )
