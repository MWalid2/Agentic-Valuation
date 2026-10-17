"""Gate 2: the assumptions as shown to the user, and edits to them, bounds-checked.

`payload` is what the gate shows: for each scenario its drivers in years 1, 5 and
10, terminal values, probability, story, rationale, preview value and flags; the
WACC build-up; and every item for review (soft bounds, model picks the statistics
did not support, the gap to the market price). `apply_edits` changes cells: each
edited value becomes `user_override` evidence with its old value in the note; the
result is checked against the hard bounds, and a set that fails is refused unless
the user overrides with a reason (the engine's own guardrails can never be
overridden: an invalid set cannot be valued).
"""

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.modeling import (
    ForecastAssumptions,
    ForecastScenarios,
    Scenario,
    WACCResult,
)
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.engine.assemble import Assembled
from dcf_agent.engine.drivers import check_coherence, industry_benchmarks, quick_dcf

Field_ = Literal[
    "revenue_growth",
    "operating_margin",
    "sales_to_capital",
    "tax_rate",
    "terminal_growth",
    "terminal_roic",
    "probability",
]
YEARLY = ("revenue_growth", "operating_margin", "sales_to_capital", "tax_rate")
SHOWN_YEARS = (0, 4, 9)


class Edit(BaseModel):
    """One cell: a scenario's driver in one forecast year (1-based) or all years."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    scenario: Scenario
    field: Field_
    value: float
    year: int | None = Field(default=None, ge=1)


class AssumptionDecision(BaseModel):
    """The user's answer at Gate 2."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    action: Literal["approve", "edit", "why", "rethink", "stop"]
    edits: tuple[Edit, ...] = ()
    override: bool = False  # accept edits that break a hard bound
    reason: str | None = None
    question: str | None = None  # for "why"
    scenario: Scenario | None = None  # for "rethink"
    note: str | None = None  # for "rethink": what the user wants reconsidered

    @model_validator(mode="after")
    def validate_action(self) -> "AssumptionDecision":
        if self.action == "edit" and not self.edits:
            raise ValueError("An edit names at least one cell")
        if self.override and not (self.reason and self.reason.strip()):
            raise ValueError("An override needs a reason")
        if self.action == "rethink" and self.scenario is None:
            raise ValueError("Re-think names a scenario")
        return self


def pct(value: float) -> str:
    return f"{value:.2%}"


def scenario_view(
    item: ForecastAssumptions, value: float | None, flags: Sequence[str]
) -> dict[str, Any]:
    years = [k for k in SHOWN_YEARS if k < len(item.year_ends)]
    return {
        "scenario": item.scenario,
        "probability": item.probability,
        "years": [item.year_ends[k].year for k in years],
        "revenue_growth": [item.revenue_growth[k].value for k in years],
        "operating_margin": [item.operating_margin[k].value for k in years],
        "sales_to_capital": [item.sales_to_capital[k].value for k in years],
        "tax_rate": [item.tax_rate[k].value for k in years],
        "terminal_growth": item.terminal_growth.value,
        "terminal_roic": item.terminal_roic.value,
        "value_per_share": value,
        "narrative": item.narrative,
        "rationale": dict(item.rationale),
        "flags": list(flags),
    }


def previews(
    scenarios: ForecastScenarios, wacc: WACCResult, assembled: Assembled, bundle: DataBundle
) -> tuple[dict[str, float | None], dict[str, list[str]]]:
    industry = industry_benchmarks(bundle)
    values: dict[str, float | None] = {}
    flags: dict[str, list[str]] = {}
    for item in scenarios.scenarios:
        preview = quick_dcf(item, wacc, assembled)
        check = check_coherence(item, wacc, industry=industry, preview=preview.result)
        values[item.scenario] = preview.per_share
        flags[item.scenario] = [*check.hard, *check.soft] + (
            [preview.error] if preview.error else []
        )
    return values, flags


def payload(
    scenarios: ForecastScenarios,
    wacc: WACCResult,
    assembled: Assembled,
    bundle: DataBundle,
    notes: Sequence[str],
    answer: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """What Gate 2 shows, as plain data for any interface."""
    values, flags = previews(scenarios, wacc, assembled, bundle)
    weighted = (
        sum(s.probability * (values[s.scenario] or 0.0) for s in scenarios.scenarios)
        if all(values[s.scenario] is not None for s in scenarios.scenarios)
        else None
    )
    market = bundle.market
    return {
        "gate": 2,
        "kind": "assumptions",
        "scenarios": [
            scenario_view(s, values[s.scenario], flags[s.scenario]) for s in scenarios.scenarios
        ],
        "weighted": weighted,
        "price": market.close.price.value if market is not None else None,
        "wacc": {
            "risk_free": wacc.rf.value,
            "erp": wacc.erp.value,
            "beta": wacc.relevered_beta.value,
            "beta_method": wacc.beta_method,
            "cost_of_equity": wacc.cost_equity.value,
            "pretax_cost_of_debt": wacc.pretax_kd.value,
            "tax_rate": wacc.tax_rate.value,
            "weight_equity": wacc.weight_equity.value,
            "wacc": wacc.wacc.value,
            "terminal_wacc": wacc.terminal_wacc.value,
        },
        "review": [n for n in notes if n.startswith(("Gate 2", "base value"))],
        "answer": answer,
        "message": message,
        "fields": list(Field_.__args__),  # type: ignore[attr-defined]
        "actions": ["approve", "edit", "why", "rethink", "stop"],
    }


def edited(value: Sourced[float], new: float, as_of: Any, why: str) -> Sourced[float]:
    return Sourced[float](
        value=new,
        prov=Provenance(
            source="user_override",
            ref="gate 2",
            as_of=as_of,
            period_end=value.prov.period_end,
            confidence="high",
            note=f"edited at Gate 2 from {value.value:.4f}" + (f": {why}" if why else ""),
        ),
    )


def apply_edits(
    scenarios: ForecastScenarios, edits: Sequence[Edit], reason: str | None = None
) -> ForecastScenarios:
    """The scenarios with the edited cells; contract checks (dates, tax, probabilities) apply."""
    items = {s.scenario: s for s in scenarios.scenarios}
    for e in edits:
        item = items[e.scenario]
        why = reason or ""
        if e.field in YEARLY:
            path = list(getattr(item, e.field))
            if e.year is not None and e.year > len(path):
                raise ValueError(f"{e.scenario} has {len(path)} forecast years, not {e.year}")
            targets = [e.year - 1] if e.year is not None else range(len(path))
            for k in targets:
                path[k] = edited(path[k], e.value, item.as_of, why)
            update: dict[str, Any] = {e.field: tuple(path)}
            if e.field == "tax_rate" and (e.year is None or e.year == len(path)):
                # The last year's tax is the marginal rate: they move together.
                update["marginal_tax_rate"] = edited(
                    item.marginal_tax_rate, e.value, item.as_of, why
                )
        elif e.field == "probability":
            update = {"probability": e.value}
        else:
            update = {e.field: edited(getattr(item, e.field), e.value, item.as_of, why)}
        items[e.scenario] = ForecastAssumptions.model_validate({**dict(item), **update})
    return ForecastScenarios(scenarios=tuple(items[s.scenario] for s in scenarios.scenarios))


def hard_failures(scenarios: ForecastScenarios, wacc: WACCResult) -> list[str]:
    return [
        f"{s.scenario}: {h}" for s in scenarios.scenarios for h in check_coherence(s, wacc).hard
    ]


CHANGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("terminal_growth", r"terminal growth|long[- ]run growth|perpetual growth"),
    ("terminal_roic", r"terminal roic|\broic\b|long[- ]run return"),
    ("sales_to_capital", r"sales[- ]to[- ]capital|reinvestment|capital intensity"),
    ("tax_rate", r"tax"),
    ("operating_margin", r"margin"),
    ("revenue_growth", r"growth"),
    ("probability", r"probability|weight"),
)


def parse_change(text: str, first_year: int) -> tuple[list[Edit], str | None] | None:
    """'change the base margin in 2028 to 25%' -> the edits, and an override reason after '!'.

    The scenario defaults to base; a year is a calendar year ("in 2028") or a forecast
    year ("year 3"); without one, every year changes. None when no field or value is found.
    """
    import re

    body, _, reason = text.partition("!")
    lowered = body.lower()
    field = next((f for f, pattern in CHANGE_FIELDS if re.search(pattern, lowered)), None)
    number = re.search(r"\b(?:to|=|at|is)\s*(-?\d+(?:\.\d+)?)\s*(%)?", lowered)
    if field is None or number is None:
        return None
    value = float(number.group(1))
    if number.group(2) or (field != "sales_to_capital" and value > 1):
        value /= 100
    scenario = next((s for s in ("bear", "bull", "base") if re.search(rf"\b{s}", lowered)), "base")
    year: int | None = None
    calendar = re.search(r"\b(?:in|for)\s+(?:fiscal\s+)?((?:19|20)\d\d)\b", lowered)
    relative = re.search(r"\byear\s+(\d{1,2})\b", lowered)
    if calendar:
        year = int(calendar.group(1)) - first_year + 1
    elif relative:
        year = int(relative.group(1))
    if year is not None and year < 1:
        return None
    if field in ("terminal_growth", "terminal_roic", "probability"):
        year = None
    edit = Edit(scenario=scenario, field=field, value=value, year=year)  # type: ignore[arg-type]
    return [edit], (reason.strip() or None)
