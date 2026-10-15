"""Engine versus Damodaran's FCFF template (recorded case; no network or LibreOffice)."""

import importlib.util
import json
from datetime import date
from math import isclose
from pathlib import Path

from test_dcf_engine import drivers

from dcf_agent.engine.dcf import discount_factor, project, timing

FIXTURE = Path("tests/fixtures/damodaran/ginzu-template.json")


def load_script():
    spec = importlib.util.spec_from_file_location(
        "check_template_parity", Path("scripts/check_template_parity.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_engine_matches_the_recorded_template_within_half_a_percent():
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = load_script().compare(case)
    assert rows and all(ok for *_, ok in rows), rows
    # Mechanics agree to rounding, not merely within the tolerance.
    assert all(abs(got - expected) <= 1e-6 * abs(expected) for _, expected, got, _ in rows)


def test_end_of_year_convention_and_rate_path():
    s, times = timing(date(2025, 12, 31), (date(2026, 12, 31), date(2027, 12, 31)), mid_year=False)
    assert (s, times) == (1.0, (1.0, 2.0))
    # Year 1 at 10%, year 2 at 20%: factor to t=2 is 1 / (1.1 x 1.2); to t=1.5 halfway into year 2.
    assert isclose(discount_factor(2.0, 1.0, (0.10, 0.20)), 1 / (1.1 * 1.2))
    assert isclose(discount_factor(1.5, 1.0, (0.10, 0.20)), 1 / (1.1 * 1.2**0.5))
    p = project(drivers(mid_year=False, wacc_path=(0.10, 0.20), terminal_wacc=0.12))
    assert isclose(p.pv_explicit, 11.5 / 1.1 + 12.65 / (1.1 * 1.2))
    assert isclose(p.pv_terminal, p.terminal_value / (1.1 * 1.2))
    # A constant path reproduces the default single-rate result.
    assert isclose(
        project(drivers(wacc_path=(0.08, 0.08))).enterprise_value,
        project(drivers()).enterprise_value,
    )
