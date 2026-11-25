#!/usr/bin/env python3
"""Does the demand multiplier drift over the scenario?

The learned model predicts the *multiplier*; the rule-based baseline holds
the median of the recent multipliers flat. If the multiplier drifts, the
baseline lags and the model tracks it — and that drift is the only reason
the model can win over a long horizon. If there is no drift, the baseline is
exact and the model can only lose, and the trust rule has to be based on
something else entirely.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import database  # noqa: E402
from app.intelligence.profiles import baseline_shape  # noqa: E402
from app.repository import read_demand_history  # noqa: E402

PROFILES = {
    "station-mirpur": "urban_high",
    "station-tongi": "industrial",
    "station-karnaphuli": "highway",
    "station-coxsbazar": "regional",
}
REGIONS = {
    "station-mirpur": 1.00,
    "station-tongi": 1.00,
    "station-karnaphuli": 1.08,
    "station-coxsbazar": 1.08,
}


async def main() -> int:
    await database.connect()
    async with database.session() as session:
        rows = await read_demand_history(session, limit=5000)
    await database.disconnect()

    by_pair: dict[tuple[str, str], list] = {}
    for row in rows:
        by_pair.setdefault((row.station_id, row.fuel_type), []).append(row)

    for (station, fuel), points in sorted(by_pair.items()):
        points = sorted(points, key=lambda p: p.tick)
        print(f"\n=== {station} / {fuel} ===")
        block = 24
        for start in range(0, len(points), block):
            chunk = points[start : start + block]
            multipliers = []
            for point in chunk:
                shape = baseline_shape(
                    profile_name=PROFILES[station],
                    fuel=fuel,
                    hour=point.sim_time.hour,
                    region_demand_factor=REGIONS[station],
                )
                if shape > 0:
                    multipliers.append(point.demand_liters / shape)
            if not multipliers:
                continue
            print(
                f"  ticks {chunk[0].tick:>4}-{chunk[-1].tick:>4}  "
                f"median={statistics.median(multipliers):.3f}  "
                f"min={min(multipliers):.3f}  max={max(multipliers):.3f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
