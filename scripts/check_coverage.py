#!/usr/bin/env python3
"""Enforce the coverage floors on the modules that matter.

A total coverage number hides where the risk is. The four modules the
brief names — the decision engine, the predictor, the simulator client
and the fallback — are the ones that decide whether the platform tells
the truth, so each one has its own floor rather than being averaged
into a total.

    python3 scripts/check_coverage.py backend/coverage.json
"""

from __future__ import annotations

import json
import sys

#: The floors. The decision engine, the predictor, the simulator client
#: and the fallback are held to 90 %; the forecasting module, which is
#: the largest and the most statistical, to 85 %.
FLOORS = {
    "app/decision/optimizer.py": 90.0,
    "app/decision/service.py": 90.0,
    "app/decision/validator.py": 90.0,
    "app/decision/fallback.py": 90.0,
    "app/intelligence/engine.py": 90.0,
    "app/intelligence/forecasting.py": 85.0,
    "app/simulator_client.py": 90.0,
}

#: The floor on the total.
TOTAL_FLOOR = 80.0


def main(argv: list[str]) -> int:
    """Check every floor and report the ones that were missed."""
    if len(argv) != 2:
        print("usage: check_coverage.py <coverage.json>", file=sys.stderr)
        return 2

    with open(argv[1]) as handle:
        coverage = json.load(handle)

    files = coverage.get("files", {})
    failures = []

    for path, floor in FLOORS.items():
        entry = files.get(path)
        if entry is None:
            failures.append(f"{path}: not measured")
            continue
        covered = entry["summary"]["percent_covered"]
        state = "ok" if covered >= floor else "BELOW FLOOR"
        print(f"{path}: {covered:.1f}% (floor {floor}%) - {state}")
        if covered < floor:
            failures.append(f"{path}: {covered:.1f}% < {floor}%")

    total = coverage.get("totals", {}).get("percent_covered", 0.0)
    print(f"TOTAL: {total:.1f}% (floor {TOTAL_FLOOR}%)")
    if total < TOTAL_FLOOR:
        failures.append(f"total: {total:.1f}% < {TOTAL_FLOOR}%")

    if failures:
        print()
        for failure in failures:
            print(f"::error::{failure}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
