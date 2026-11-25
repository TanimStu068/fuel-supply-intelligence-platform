#!/usr/bin/env python3
"""How long does a training run take, at the sizes the demo produces?

The fix for the trust oscillation needs a model per replay window, so the
cost of one training run is the number that decides whether it is
affordable.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import database  # noqa: E402
from app.intelligence.forecasting import (  # noqa: E402
    HistoryPoint,
    LearnedPredictor,
    StationContext,
)
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

    history = [
        HistoryPoint(
            station_id=row.station_id,
            fuel_type=row.fuel_type,
            tick=row.tick,
            sim_time=row.sim_time,
            demand_liters=row.demand_liters,
            served_liters=row.served_liters,
            unmet_liters=row.unmet_liters,
        )
        for row in rows
    ]

    by_pair: dict[tuple[str, str], list[HistoryPoint]] = {}
    for point in history:
        by_pair.setdefault((point.station_id, point.fuel_type), []).append(point)

    contexts = {
        station_id: StationContext(
            station_id=station_id,
            profile_name=PROFILES[station_id],
            region_demand_factor=REGIONS[station_id],
            demand_multiplier=1.0,
            current_tick=max(p.tick for p in points),
            current_sim_time=max(points, key=lambda p: p.tick).sim_time,
            tick_minutes=15,
        )
        for (station_id, _), points in by_pair.items()
    }

    print(f"history rows: {len(history)}")
    per_pair = {len(points) for points in by_pair.values()}
    print(f"rows per pair: {sorted(per_pair)}")
    print(f"pairs: {len(by_pair)}")

    predictor = LearnedPredictor()
    started = time.perf_counter()
    predictor.train(history, contexts)
    print(f"one full training run: {time.perf_counter() - started:.2f}s")

    # A per-window training set: the prefix before one window.
    first_pair = next(iter(by_pair.values()))
    prefix = first_pair[: max(0, len(first_pair) - 96)]
    started = time.perf_counter()
    try:
        predictor.train(prefix, contexts)
        print(f"one window-sized training run: {time.perf_counter() - started:.2f}s")
    except Exception as exc:
        print(f"prefix training failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
