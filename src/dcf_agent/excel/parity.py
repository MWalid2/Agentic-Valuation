"""Recalculate a workbook with LibreOffice and compare it with the engine.

A temporary LibreOffice profile forces recalculation on load (the user's own
profile is never touched). Parity means value per share equal to the cent.
"""

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from dcf_agent.excel.workbook import Cells

CANDIDATES = (
    Path("C:/Program Files/LibreOffice/program/soffice.exe"),
    Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
    Path("/usr/bin/soffice"),
    Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
)
ALWAYS_RECALCULATE = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"
 xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Calc/Formula/Load">
 <prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop>
</item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load">
 <prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop>
</item>
</oor:items>
"""
CENT = 0.01


class ParityError(RuntimeError):
    pass


@dataclass(frozen=True)
class Check:
    label: str
    engine: float
    workbook: float | None
    tolerance: float

    @property
    def passed(self) -> bool:
        return self.workbook is not None and abs(self.workbook - self.engine) <= self.tolerance


def find_soffice() -> Path | None:
    configured = os.environ.get("DCF_SOFFICE")
    if configured:
        return Path(configured)
    found = shutil.which("soffice")
    if found:
        return Path(found)
    return next((p for p in CANDIDATES if p.exists()), None)


def recalculate(path: Path, out_dir: Path, *, timeout: float = 180) -> Path:
    """Open, recalculate and save a copy with LibreOffice in headless mode."""
    soffice = find_soffice()
    if soffice is None:
        raise ParityError("LibreOffice (soffice) not found; set DCF_SOFFICE to its path")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dcf-lo-profile-") as profile:
        user = Path(profile) / "user"
        user.mkdir(parents=True)
        (user / "registrymodifications.xcu").write_text(ALWAYS_RECALCULATE, encoding="utf-8")
        command = [
            str(soffice),
            f"-env:UserInstallation={Path(profile).as_uri()}",
            "--headless",
            "--norestore",
            "--calc",
            "--convert-to",
            "xlsx:Calc MS Excel 2007 XML",
            "--outdir",
            str(out_dir),
            str(path),
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    result = out_dir / path.name
    if completed.returncode != 0 or not result.exists():
        raise ParityError(f"LibreOffice recalculation failed: {completed.stderr.strip()[:300]}")
    return result


def read(path: Path, reference: str) -> float | None:
    sheet, cell = reference.rsplit("!", 1)
    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        value = workbook[sheet.strip("'")][cell.replace("$", "")].value
    finally:
        workbook.close()
    return float(value) if isinstance(value, (int, float)) else None


def verify(
    recalculated: Path,
    cells: Cells,
    *,
    value_per_share: dict[str, float],
    enterprise_value: dict[str, float],
    weighted: float,
    wacc: float,
) -> list[Check]:
    checks = [
        Check(f"{s} value per share", v, read(recalculated, cells.value_per_share[s]), CENT)
        for s, v in value_per_share.items()
    ]
    checks += [
        Check(
            f"{s} enterprise value",
            v,
            read(recalculated, cells.enterprise_value[s]),
            max(1.0, abs(v) * 1e-9),
        )
        for s, v in enterprise_value.items()
    ]
    checks.append(
        Check("weighted value per share", weighted, read(recalculated, cells.weighted), CENT)
    )
    checks.append(Check("WACC", wacc, read(recalculated, cells.wacc), 1e-9))
    return checks
