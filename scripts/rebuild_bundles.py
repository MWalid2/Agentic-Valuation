"""Rebuild every bundle in a cohort with the current code, then run the cohort checker."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, default=ROOT / "scripts/cohorts/reference.json")
    parser.add_argument(
        "--online", action="store_true", help="Allow network requests for missing sources"
    )
    args = parser.parse_args()
    entries = json.loads(args.cohort.read_text(encoding="utf-8"))
    failed = []
    for entry in entries:
        command = [sys.executable, str(ROOT / "scripts/collect_data.py")]
        for key in (
            "ticker",
            "cik",
            "name",
            "as_of",
            "industry",
            "country",
            "sec_snapshot",
            "out",
        ):
            if key in entry:
                command += ["--" + key.replace("_", "-"), str(entry[key])]
        if not args.online:
            command.append("--offline")
        print(f"== {entry['ticker']}", flush=True)
        if subprocess.run(command, cwd=ROOT, check=False).returncode != 0:
            failed.append(entry["ticker"])
    checker = [sys.executable, str(ROOT / "scripts/check_data_bundles.py")]
    checker += [entry["out"] for entry in entries]
    checked = subprocess.run(checker, cwd=ROOT, check=False).returncode == 0
    if failed:
        print("Collection reported blocking issues: " + ", ".join(failed))
    if failed or not checked:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
