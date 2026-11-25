#!/usr/bin/env python3
"""Why does the honest horizon score turn against the model?

The verdict is stable now, but it still flips on the long run. This probe
prints the per-window detail — what the model predicted, what the rule
predicted, and what actually happened — so the flip can be *explained*
rather than tuned away.

    DATABASE_URL=postgresql+asyncpg://... python scripts/horizon_detail_probe.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import database  # noqa: E402
from app.intelligence.forecasting import (  # noqa: E402
    HistoryPoint,
    LearnedPredictor,
    StationContext,
    HorizonValidator,
)
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

    validator = HorizonValidator(horizon_ticks=HORIZON, windows=4)

    # One station/fuel pair in detail, then the summary over all of them.
    print(f"{'pair':34s} {'win':>4s} {'model':>12s} {'rule':>12s} {'actual':>12s} {'verdict':>8s}")
    totals_model: list[float] = []
    totals_rule: list[float] = []
    windows_seen = 0

    for (station_id, fuel), points in sorted(by_pair.items()):
        points = sorted(points, key=lambda p: p.tick)
        context = contexts[station_id]
        if len(points) < HORIZON + 4:
            continue

        for index in range(validator.windows):
            end = len(points) - index * HORIZON
            start = end - HORIZON
            if start < 4:
                break

            origin = points[start - 1]
            replay = points[start:end]
            before = [p for p in points if p.tick <= origin.tick]
            actual = sum(p.demand_liters for p in replay)

            window_context = replace(
                context, current_tick=origin.tick, current_sim_time=origin.sim_time
            )

            model = validator._model_for_prefix(before, contexts)
            if model is None:
                continue
            try:
                forecast = model.forecast(
                    context=window_context,
                    fuel=fuel,
                    horizon_ticks=HORIZON,
                    history=before,
                )
            except Exception:
                continue
            model_total = sum(p.predicted_demand for p in forecast.points)

            from app.intelligence.forecasting import _historical_multipliers
            from app.intelligence.profiles import baseline_shape

            multipliers = _historical_multipliers(before, context, fuel)
            if multipliers:
                ordered = sorted(multipliers.items())
                window = [value for _, value in ordered[-12:]]
                middle = len(window) // 2
                multiplier = (
                    window[middle]
                    if len(window) % 2 == 1
                    else (window[middle - 1] + window[middle]) / 2
                )
            else:
                multiplier = 1.0

            rule_total = 0.0
            for point in replay:
                shape = baseline_shape(
                    profile_name=context.profile_name,
                    fuel=fuel,
                    hour=point.sim_time.hour,
                    region_demand_factor=context.region_demand_factor,
                )
                rule_total += shape * multiplier

            verdict = "model" if abs(model_total - actual) < abs(rule_total - actual) else "rule"
            print(
                f"{station_id + '/' + fuel:34s} {index:>4d} {model_total:>12.1f} "
                f"{rule_total:>12.1f} {actual:>12.1f} {verdict:>8s}"
            )
            totals_model.append(abs(model_total - actual))
            totals_rule.append(abs(rule_total - actual))
            windows_seen += 1

    if totals_model:
        model_mean = sum(totals_model) / len(totals_model)
        rule_mean = sum(totals_rule) / len(totals_rule)
        wins = sum(1 for m, r in zip(totals_model, totals_rule) if m < r)
        print(f"\nwindows: {windows_seen}")
        print(f"model error {model_mean:.1f} L, rule error {rule_mean:.1f} L")
        print(f"model wins {wins}/{len(totals_model)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
