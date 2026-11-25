#!/usr/bin/env python3
"""How stable is the model's acceptance?

The platform only uses the learned model when it has demonstrably beaten the
rule-based baseline over the forecast horizon. The horizon validator scores
the model on **one** replay window — the last 96 ticks of the stored history.
That window slides with the daily demand cycle, and the difference between
two estimators' errors on a single window is mostly noise, so the verdict
flips: the same world, the same code, the model trusted on one retrain and
discarded on the next.

This probe measures the distribution. For each (station, fuel) pair it
scores the model and the honest baseline over several replay windows and
reports:

* the per-window relative difference, and
* the verdict when the windows are averaged.

Usage:
    DATABASE_URL=postgresql+asyncpg://... python scripts/model_stability_probe.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys

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

    predictor = LearnedPredictor()
    predictor.train(history, contexts)

    #: How many replay windows to score, walking backwards from the newest.
    windows = 4

    print(f"history rows: {len(history)}, pairs: {len(by_pair)}, windows: {windows}\n")
    print(f"{'pair':42s} {'window':>8s} {'model':>10s} {'baseline':>10s} {'diff':>9s}")

    all_model: list[float] = []
    all_base: list[float] = []
    per_window_diff: dict[int, list[float]] = {index: [] for index in range(windows)}

    for (station_id, fuel), points in sorted(by_pair.items()):
        points = sorted(points, key=lambda p: p.tick)
        context = contexts[station_id]
        if len(points) < HORIZON + 4:
            continue

        for index in range(windows):
            end = len(points) - index * HORIZON
            start = end - HORIZON
            if start < 4:
                break

            origin = points[start - 1]
            replay = points[start:end]
            before = [p for p in points if p.tick <= origin.tick]
            actual_total = sum(p.demand_liters for p in replay)

            try:
                forecast = predictor.forecast(
                    context=context,
                    fuel=fuel,
                    horizon_ticks=HORIZON,
                    history=before,
                )
            except Exception:
                continue

            model_total = sum(point.predicted_demand for point in forecast.points)

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

            model_error = abs(model_total - actual_total)
            baseline_error = abs(baseline_total - actual_total)
            diff = (model_error - baseline_error) / max(baseline_error, 1e-6)

            per_window_diff[index].append(diff)
            all_model.append(model_error)
            all_base.append(baseline_error)

            print(
                f"{station_id + '/' + fuel:42s} {index:>8d} "
                f"{model_error:>10.1f} {baseline_error:>10.1f} {diff:>+8.1%}"
            )

    print()
    for index in range(windows):
        diffs = per_window_diff[index]
        if not diffs:
            continue
        wins = sum(1 for diff in diffs if diff < 0)
        print(
            f"window {index}: mean diff {statistics.mean(diffs):+.1%}   "
            f"model wins {wins}/{len(diffs)}"
        )

    print()
    print(f"single window (index 0) mean diff: {statistics.mean(per_window_diff[0]):+.1%}")
    pooled_model = statistics.mean(all_model)
    pooled_base = statistics.mean(all_base)
    print(
        f"all windows pooled  : model {pooled_model:8.1f} L   "
        f"baseline {pooled_base:8.1f} L   diff "
        f"{(pooled_model - pooled_base) / max(pooled_base, 1e-6):+.1%}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
