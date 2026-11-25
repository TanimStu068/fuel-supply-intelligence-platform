#!/usr/bin/env python3
"""Does the refit make the horizon verdict stable?

The fix refits the model on the history that precedes each replay
window. This measures the *spread* of the per-window verdicts, leaky
against honest, over the stored history.

The claim the fix rests on: the leaky score's windows disagree with each
other, and the honest score's windows agree. That is the difference
between a verdict that is a coin toss and one that is a verdict.

    DATABASE_URL=postgresql+asyncpg://... python scripts/window_stability_probe.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import database  # noqa: E402
from app.intelligence.forecasting import (  # noqa: E402
    HistoryPoint,
    LearnedPredictor,
    StationContext,
)
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

    leaky_predictor = LearnedPredictor()
    leaky_predictor.train(history, contexts, score_horizon=False)

    # Per pair, the verdict of each window, leaky and honest.
    leaky_verdicts: list[list[bool]] = []
    honest_verdicts: list[list[bool]] = []

    for (station_id, fuel), points in sorted(by_pair.items()):
        points = sorted(points, key=lambda p: p.tick)
        context = contexts[station_id]
        if len(points) < HORIZON + 4:
            continue

        leaky_row: list[bool] = []
        honest_row: list[bool] = []

        for index in range(WINDOWS):
            end = len(points) - index * HORIZON
            start = end - HORIZON
            if start < 4:
                break

            origin = points[start - 1]
            replay = points[start:end]
            before = [p for p in points if p.tick <= origin.tick]
            actual_total = sum(p.demand_liters for p in replay)

            window_context = replace(
                context,
                current_tick=origin.tick,
                current_sim_time=origin.sim_time,
            )

            baseline_total = _baseline_total(replay, before, context, fuel)

            for label, predictor in (("leaky", leaky_predictor), ("honest", None)):
                if label == "honest":
                    predictor = LearnedPredictor()
                    try:
                        predictor.train(before, contexts, score_horizon=False)
                    except Exception:
                        continue
                try:
                    forecast = predictor.forecast(
                        context=window_context,
                        fuel=fuel,
                        horizon_ticks=HORIZON,
                        history=before,
                    )
                except Exception:
                    continue
                model_total = sum(point.predicted_demand for point in forecast.points)
                verdict = abs(model_total - actual_total) < abs(baseline_total - actual_total)
                if label == "leaky":
                    leaky_row.append(verdict)
                else:
                    honest_row.append(verdict)

        if leaky_row:
            leaky_verdicts.append(leaky_row)
        if honest_row:
            honest_verdicts.append(honest_row)

    def report(name: str, rows: list[list[bool]]) -> None:
        unstable = sum(1 for row in rows if len(set(row)) > 1)
        flat = [verdict for row in rows for verdict in row]
        print(
            f"{name}: {unstable}/{len(rows)} pairs have windows that disagree, "
            f"{statistics.mean(flat):.0%} of windows say the model wins"
        )

    print(f"pairs scored: {len(leaky_verdicts)}")
    report("leaky ", leaky_verdicts)
    report("honest", honest_verdicts)
    return 0


def _baseline_total(replay, before, context, fuel) -> float:
    from app.intelligence.forecasting import _historical_multipliers
    from app.intelligence.profiles import baseline_shape

    multipliers = _historical_multipliers(before, context, fuel)
    if multipliers:
        ordered = sorted(multipliers.items())
        window = [value for _, value in ordered[-12:]]
        middle = len(window) // 2
        if len(window) % 2 == 1:
            multiplier = window[middle]
        else:
            multiplier = (window[middle - 1] + window[middle]) / 2
    else:
        multiplier = 1.0

    total = 0.0
    for point in replay:
        shape = baseline_shape(
            profile_name=context.profile_name,
            fuel=fuel,
            hour=point.sim_time.hour,
            region_demand_factor=context.region_demand_factor,
        )
        total += shape * multiplier
    return total


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
