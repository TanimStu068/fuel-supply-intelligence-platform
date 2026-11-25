#!/usr/bin/env python3
"""Is the horizon score honest?

The validator scores the model on a replay window and compares it with the
rule-based baseline. It hands the model the history *before* the window —
but the **estimator itself** was trained on the *whole* history, including
the window. So the model was asked to predict rows it had already memorised.

This probe measures both ways:

* **leaky** — the model trained on everything, scored on the tail (what
  the validator does today);
* **honest** — a model trained only on the prefix before the window, scored
  on the window.

If the two agree, the leak is harmless and the published numbers stand. If
they do not, the published "the model beats the baseline by X %" figure is
measuring the model against itself.

    DATABASE_URL=postgresql+asyncpg://... python scripts/horizon_honesty_probe.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import database  # noqa: E402
from app.intelligence.forecasting import (  # noqa: E402
    HistoryPoint,
    LearnedPredictor,
    StationContext,
    _historical_multipliers,
)
from app.intelligence.profiles import baseline_shape  # noqa: E402
from app.repository import read_demand_history  # noqa: E402

HORIZON = 96
WINDOWS = 4
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


def honest_multiplier(before: list[HistoryPoint], context: StationContext, fuel: str) -> float:
    """The baseline's multiplier, computed only from history before the window."""
    multipliers = _historical_multipliers(before, context, fuel)
    if not multipliers:
        return 1.0
    ordered = sorted(multipliers.items())
    window = [value for _, value in ordered[-12:]]
    middle = len(window) // 2
    if len(window) % 2 == 1:
        return window[middle]
    return (window[middle - 1] + window[middle]) / 2


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

    # The context carries the station's *authoritative* demand multiplier —
    # the one the simulator is actually running the world at. It is read
    # from the live state rather than assumed to be 1.0, because during a
    # demand spike it is 3.0, and a context that says 1.0 would describe a
    # world the simulator is not running.
    multiplier_by_station = await _live_demand_multipliers()

    contexts = {
        station_id: StationContext(
            station_id=station_id,
            profile_name=PROFILES[station_id],
            region_demand_factor=REGIONS[station_id],
            demand_multiplier=multiplier_by_station.get(station_id, 1.0),
            current_tick=max(p.tick for p in points),
            current_sim_time=max(points, key=lambda p: p.tick).sim_time,
            tick_minutes=15,
        )
        for (station_id, _), points in by_pair.items()
    }

    leaky_predictor = LearnedPredictor()
    leaky_predictor.train(history, contexts)

    leaky_model: list[float] = []
    leaky_base: list[float] = []
    honest_model: list[float] = []
    honest_base: list[float] = []

    started = time.perf_counter()

    for (station_id, fuel), points in sorted(by_pair.items()):
        points = sorted(points, key=lambda p: p.tick)
        context = contexts[station_id]
        if len(points) < HORIZON + 4:
            continue

        for index in range(WINDOWS):
            end = len(points) - index * HORIZON
            start = end - HORIZON
            if start < 4:
                break

            origin = points[start - 1]
            replay = points[start:end]
            before = [p for p in points if p.tick <= origin.tick]
            actual_total = sum(p.demand_liters for p in replay)

            # The forecast must be made **from the origin of the window**,
            # not from the newest tick. A context that still points at the
            # end of the history would ask the model for a forecast that
            # starts after the window it is being scored on, and the
            # comparison would be measuring nothing.
            window_context = replace(
                context,
                current_tick=origin.tick,
                current_sim_time=origin.sim_time,
            )

            # --- the baseline, which never sees the window ------------------
            multiplier = honest_multiplier(before, context, fuel)
            baseline_total = 0.0
            for point in replay:
                shape = baseline_shape(
                    profile_name=context.profile_name,
                    fuel=fuel,
                    hour=point.sim_time.hour,
                    region_demand_factor=context.region_demand_factor,
                )
                baseline_total += shape * multiplier

            # --- the leaky model --------------------------------------------
            try:
                forecast = leaky_predictor.forecast(
                    context=window_context,
                    fuel=fuel,
                    horizon_ticks=HORIZON,
                    history=before,
                )
                leaky_total = sum(point.predicted_demand for point in forecast.points)
            except Exception:
                continue

            # --- the honest model -------------------------------------------
            honest_predictor = LearnedPredictor()
            honest_predictor.train(before, contexts)
            try:
                forecast = honest_predictor.forecast(
                    context=window_context,
                    fuel=fuel,
                    horizon_ticks=HORIZON,
                    history=before,
                )
                honest_total = sum(point.predicted_demand for point in forecast.points)
            except Exception:
                continue

            leaky_model.append(abs(leaky_total - actual_total))
            leaky_base.append(abs(baseline_total - actual_total))
            honest_model.append(abs(honest_total - actual_total))
            honest_base.append(abs(baseline_total - actual_total))

    elapsed = time.perf_counter() - started

    print(f"history rows: {len(history)}, pairs: {len(by_pair)}, windows: {WINDOWS}")
    print(f"elapsed: {elapsed:.1f}s\n")

    leaky_m = statistics.mean(leaky_model)
    leaky_b = statistics.mean(leaky_base)
    honest_m = statistics.mean(honest_model)
    honest_b = statistics.mean(honest_base)

    print(f"{'model':<12} {'error':>12} {'baseline':>12} {'difference':>12}")
    print(f"{'leaky':<12} {leaky_m:>12.1f} {leaky_b:>12.1f} {(leaky_m - leaky_b) / max(leaky_b, 1e-6):>11.1%}")
    print(f"{'honest':<12} {honest_m:>12.1f} {honest_b:>12.1f} {(honest_m - honest_b) / max(honest_b, 1e-6):>11.1%}")

    leaky_wins = sum(1 for m, b in zip(leaky_model, leaky_base) if m < b)
    honest_wins = sum(1 for m, b in zip(honest_model, honest_base) if m < b)
    print(f"\nmodel wins {leaky_wins}/{len(leaky_model)} leaky, {honest_wins}/{len(honest_model)} honest")
    return 0


async def _live_demand_multipliers() -> dict[str, float]:
    """The demand multiplier each station is currently running at."""
    import os

    import httpx

    url = os.environ.get("SIMULATOR_BASE_URL", "http://localhost:8100")
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{url}/v1/stations")
        response.raise_for_status()
        stations = response.json()
    return {station["id"]: float(station.get("demand_multiplier", 1.0)) for station in stations}


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
