#!/usr/bin/env python3
"""Does the learned model extrapolate to ticks it has never seen?

This is the question that matters, because it is the one the platform asks
every cycle: the model is trained on the history up to tick T and is then
asked to forecast ticks T+1 .. T+96.

The feature vector includes the **absolute** tick number. A gradient-boosted
tree cannot extrapolate past the largest value it was trained on, so a
feature that runs to 5 000 during training contributes nothing to a forecast
about tick 5 001 — and the splits it *did* learn on it are noise.

This probe trains on the first part of the history and forecasts the part
after it, twice: once with the absolute tick, and once with the tick's
position within the simulated day, which is bounded and therefore usable.

Usage:
    DATABASE_URL=postgresql+asyncpg://... python scripts/extrapolation_probe.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import database  # noqa: E402
from app.intelligence import forecasting  # noqa: E402
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

ORIGINAL_BUILD = forecasting.build_features


def absolute_build(history, context, fuel, target_tick, *, lag_values=None, multipliers=None):
    """The features the platform used to ship: the **absolute** tick.

    This has to be written out rather than borrowed from the shipped
    builder, because the shipped builder now uses the tick's position
    within the day — and borrowing it would make both arms of this probe
    measure the same thing, which is how the comparison quietly became
    meaningless.
    """
    row = ORIGINAL_BUILD(
        history,
        context,
        fuel,
        target_tick,
        lag_values=lag_values,
        multipliers=multipliers,
    )
    if row is None:
        return None
    row[0] = float(target_tick)
    return row


def cyclic_build(history, context, fuel, target_tick, *, lag_values=None, multipliers=None):
    """The same features, but the tick is its position within the day."""
    row = ORIGINAL_BUILD(
        history,
        context,
        fuel,
        target_tick,
        lag_values=lag_values,
        multipliers=multipliers,
    )
    if row is None:
        return None
    row[0] = float(target_tick % 96)
    return row


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

    def extrapolate(predictor: LearnedPredictor, label: str) -> None:
        """Train on the past, forecast the horizon just after it."""
        model_errors: list[float] = []
        baseline_errors: list[float] = []
        wins = 0

        for (station_id, fuel), points in sorted(by_pair.items()):
            points = sorted(points, key=lambda p: p.tick)
            context = contexts[station_id]
            if len(points) < HORIZON * 3:
                continue

            # The split: everything up to the origin is training data,
            # the next 96 ticks are the forecast the platform would make.
            origin = points[-HORIZON - 1]
            replay = points[-HORIZON:]
            before = [p for p in points if p.tick <= origin.tick]
            actual_total = sum(p.demand_liters for p in replay)

            try:
                predictor.train(before, contexts)
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
            model_errors.append(model_error)
            baseline_errors.append(baseline_error)
            if model_error < baseline_error:
                wins += 1

        if not model_errors:
            print(f"{label}: not enough history")
            return

        model_mae = statistics.mean(model_errors)
        baseline_mae = statistics.mean(baseline_errors)
        print(
            f"{label:34s} model {model_mae:9.1f} L   baseline {baseline_mae:9.1f} L"
            f"   diff {(model_mae - baseline_mae) / max(baseline_mae, 1e-6):+8.1%}"
            f"   wins {wins}/{len(model_errors)}"
        )

    print(f"history rows: {len(history)}, pairs: {len(by_pair)}\n")
    print("Extrapolation — trained on the past, forecasting the next 96 ticks:\n")

    forecasting.build_features = absolute_build
    extrapolate(LearnedPredictor(), "absolute tick (as first shipped)")

    forecasting.build_features = cyclic_build
    extrapolate(LearnedPredictor(), "tick within the day")

    forecasting.build_features = ORIGINAL_BUILD
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
