"""Football-field chart as SVG, drawn in code (no plotting dependency).

One horizontal bar per range (DCF scenarios, comps, the widest driver, the 52-week
price), its low and high labelled, an optional mark inside it, and a dashed line at
the current price.
"""

from collections.abc import Sequence
from html import escape

from dcf_agent.report.summary import Range

WIDTH, LEFT, RIGHT, ROW, TOP = 760, 250, 40, 44, 48
COLORS = ("#2f5597", "#70ad47", "#ed7d31", "#7f7f7f", "#a5a5a5")


def football_field(
    ranges: Sequence[Range], price: float | None, title: str, *, currency: str = "$"
) -> str:
    if not ranges:
        return ""
    values = [v for r in ranges for v in (r.low, r.high)] + ([price] if price else [])
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.08 or max(abs(hi), 1.0) * 0.1
    lo, hi = lo - pad, hi + pad
    span = WIDTH - LEFT - RIGHT
    height = TOP + ROW * len(ranges) + 40

    def x(v: float) -> float:
        return LEFT + (v - lo) / (hi - lo) * span

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" '
        f'width="{WIDTH}" height="{height}" font-family="Segoe UI, Arial, sans-serif" '
        'font-size="12" role="img">',
        f"<title>{escape(title)}</title>",
        f'<text x="{LEFT}" y="22" font-size="14" font-weight="bold">{escape(title)}</text>',
    ]
    for k, r in enumerate(ranges):
        y = TOP + ROW * k
        color = COLORS[k % len(COLORS)]
        x0, x1 = x(min(r.low, r.high)), x(max(r.low, r.high))
        parts += [
            f'<text x="{LEFT - 10}" y="{y + 17}" text-anchor="end">{escape(r.label)}</text>',
            f'<rect x="{x0:.1f}" y="{y + 4}" width="{max(x1 - x0, 2):.1f}" height="18" '
            f'fill="{color}" opacity="0.85"/>',
            f'<text x="{x0:.1f}" y="{y + 36}" text-anchor="start" fill="#404040">'
            f"{currency}{r.low:,.2f}</text>",
            f'<text x="{x1:.1f}" y="{y + 36}" text-anchor="end" fill="#404040">'
            f"{currency}{r.high:,.2f}</text>",
        ]
        if r.mark is not None:
            m = x(r.mark)
            parts.append(
                f'<line x1="{m:.1f}" x2="{m:.1f}" y1="{y + 1}" y2="{y + 25}" '
                'stroke="#000" stroke-width="2"/>'
            )
    if price:
        p = x(price)
        parts += [
            f'<line x1="{p:.1f}" x2="{p:.1f}" y1="{TOP - 8}" y2="{height - 28}" '
            'stroke="#c00000" stroke-width="1.5" stroke-dasharray="5,4"/>',
            f'<text x="{p:.1f}" y="{height - 12}" text-anchor="middle" fill="#c00000">'
            f"price {currency}{price:,.2f}</text>",
        ]
    parts.append("</svg>")
    return "\n".join(parts)
