#!/usr/bin/env python3
"""Whether the learned model beats an *honest* baseline.

The horizon validator compares the learned model with the rule-based
baseline over the same replay window. This script scores both directly
against the stored history, and scores the baseline twice: once the way
the validator used to (its multiplier taken from the end of the window it
is predicting) and once honestly (only the history before the window).

The gap between the two baseline scores is the size of the look-ahead bias
that was hiding a better model.

Usage:
    DATABASE_URL=postgresql+asyncpg://... python scripts/horizon_probe.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

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

    model_errors: list[float] = []
    lookahead_errors: list[float] = []
    honest_errors: list[float] = []

    for (station_id, fuel), points in sorted(by_pair.items()):
        points.sort(key=lambda p: p.tick)
        context = contexts[station_id]
        if len(points) < HORIZON + 4:
            continue

        origin = points[-HORIZON - 1]
        replay = points[-HORIZON:]
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

        def shape_of(point: HistoryPoint) -> float:
            return baseline_shape(
                profile_name=context.profile_name,
                fuel=fuel,
                hour=point.sim_time.hour,
                region_demand_factor=context.region_demand_factor,
            )

        # (a) The baseline as the validator used to score it: its
        #     multiplier comes from the END of the replay window.
        lookahead_multiplier = 1.0
        recent = points[-12:]
        if recent:
            usable = [
                p.demand_liters / s for p, s in zip(recent, map(shape_of, recent), strict=True) if s > 0
            ]
            if usable:
                lookahead_multiplier = sum(usable) / len(usable)

        # (b) The honest baseline: the same rule the live baseline uses,
        #     computed only from the history before the window.
        multipliers = _historical_multipliers(before, context, fuel)
        honest_multiplier = 1.0
        if multipliers:
            ordered = sorted(multipliers.items())
            window = [value for _, value in ordered[-12:]]
            middle = len(window) // 2
            honest_multiplier = (
                window[middle]
                if len(window) % 2 == 1
                else (window[middle - 1] + window[middle]) / 2
            )

        def total(multiplier: float) -> float:
            return sum(shape_of(p) * multiplier for p in replay)

        model_errors.append(abs(model_total - actual_total))
        lookahead_errors.append(abs(total(lookahead_multiplier) - actual_total))
        honest_errors.append(abs(total(honest_multiplier) - actual_total))

    if not model_errors:
        print("not enough history to score a horizon")
        return 1

    print(f"pairs scored: {len(model_errors)}")
    print(f"learned model          horizon MAE: {statistics.mean(model_errors):9.1f} L")
    print(f"baseline (look-ahead)  horizon MAE: {statistics.mean(lookahead_errors):9.1f} L")
    print(f"baseline (honest)      horizon MAE: {statistics.mean(honest_errors):9.1f} L")
    print()
    if statistics.mean(model_errors) < statistics.mean(honest_errors):
        print("VERDICT: the learned model beats an honest baseline.")
    else:
        print("VERDICT: the learned model does NOT beat an honest baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
