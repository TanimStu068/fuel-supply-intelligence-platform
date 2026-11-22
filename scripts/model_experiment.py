#!/usr/bin/env python3
"""Compare demand-model configurations on the real stored history.

The point is not to pick the model with the best score on paper. It is to
find out *why* the learned model was losing to the deterministic rule, and
whether that was a real property of the data or a defect in the model.

Each configuration is scored on exactly the same time-based split, and the
rule-based baseline is scored on the same rows, so the numbers are
comparable.

Usage:
    python scripts/model_experiment.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor  # noqa: E402
from sklearn.linear_model import Ridge  # noqa: E402
from sklearn.metrics import mean_absolute_error  # noqa: E402

from app.db import Database  # noqa: E402
from app.intelligence.forecasting import (  # noqa: E402
    HistoryPoint,
    StationContext,
    _baseline_multiplier,
    _historical_multipliers,
    _historical_shape,
    build_features,
    effective_multiplier,
)
from app.repository import read_demand_history  # noqa: E402

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://fuel:change-me-in-env@localhost:55432/fuel"
)


async def load_rows() -> list:
    """Read the stored demand history."""
    db = Database()
    await db.connect()
    try:
        async with db.session() as session:
            return await read_demand_history(session, limit=5000)
    finally:
        await db.disconnect()


def contexts_for(rows: list) -> dict[str, StationContext]:
    """Build a station context per station from the stored rows."""
    contexts: dict[str, StationContext] = {}

    for row in rows:
        if row.station_id in contexts:
            continue
        contexts[row.station_id] = StationContext(
            station_id=row.station_id,
            profile_name="urban_high",
            region_demand_factor=1.0,
            demand_multiplier=1.0,
            current_tick=row.tick,
            tick_minutes=15,
            current_sim_time=row.sim_time,
        )

    return contexts


def build_dataset(rows: list, contexts: dict[str, StationContext]):
    """The feature matrix, the targets and the baseline's predictions."""
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

    features: list[list[float]] = []
    targets: list[float] = []
    baseline: list[float] = []

    cache: dict[tuple[str, str], dict[int, float] | None] = {}

    for point in history:
        context = contexts.get(point.station_id)
        if context is None:
            continue

        key = (point.station_id, point.fuel_type)
        if key not in cache:
            cache[key] = _historical_multipliers(history, context, point.fuel_type)
        multipliers = cache[key]

        vector = build_features(
            history, context, point.fuel_type, point.tick, multipliers=multipliers
        )
        if vector is None:
            continue

        features.append(vector)
        shape = _historical_shape(context, point.fuel_type, point)
        targets.append(effective_multiplier(point.demand_liters, shape))
        baseline.append(_baseline_multiplier(multipliers, point.tick))

    return features, targets, baseline


def score(name, estimator, x_train, y_train, x_valid, y_valid, baseline_valid):
    """Score one configuration on the validation split."""
    estimator.fit(x_train, y_train)
    predictions = estimator.predict(x_valid)
    mae = float(mean_absolute_error(y_valid, predictions))
    baseline_mae = float(mean_absolute_error(y_valid, baseline_valid))
    verdict = "learned wins" if mae < baseline_mae else "baseline wins"
    print(
        f"{name:<48} mae={mae:.5f}  baseline={baseline_mae:.5f}  "
        f"ratio={mae / baseline_mae:.2f}  {verdict}"
    )
    return {
        "name": name,
        "validation_mae": mae,
        "baseline_validation_mae": baseline_mae,
        "ratio": mae / baseline_mae,
    }


def main() -> int:
    rows = asyncio.run(load_rows())
    print(f"loaded {len(rows)} demand observations")
    if not rows:
        print("no history stored yet - run the simulator first")
        return 1

    contexts = contexts_for(rows)
    features, targets, baseline = build_dataset(rows, contexts)
    print(f"dataset: {len(features)} rows, {len(features[0])} features")

    split = int(len(features) * 0.8)
    x_train, x_valid = features[:split], features[split:]
    y_train, y_valid = targets[:split], targets[split:]
    baseline_valid = baseline[split:]
    print(f"split: {len(x_train)} train / {len(x_valid)} validation\n")

    results = [
        score(
            "GradientBoosting depth=4 (current)",
            GradientBoostingRegressor(
                n_estimators=200,
                learning_rate=0.05,
                max_depth=4,
                subsample=0.9,
                random_state=42,
            ),
            x_train,
            y_train,
            x_valid,
            y_valid,
            baseline_valid,
        ),
        score(
            "GradientBoosting depth=2",
            GradientBoostingRegressor(
                n_estimators=100,
                learning_rate=0.05,
                max_depth=2,
                subsample=0.9,
                random_state=42,
            ),
            x_train,
            y_train,
            x_valid,
            y_valid,
            baseline_valid,
        ),
        score(
            "GradientBoosting depth=1, 50 trees",
            GradientBoostingRegressor(
                n_estimators=50,
                learning_rate=0.05,
                max_depth=1,
                subsample=0.9,
                random_state=42,
            ),
            x_train,
            y_train,
            x_valid,
            y_valid,
            baseline_valid,
        ),
        score(
            "GradientBoosting depth=2, min_samples_leaf=50",
            GradientBoostingRegressor(
                n_estimators=100,
                learning_rate=0.05,
                max_depth=2,
                min_samples_leaf=50,
                subsample=0.9,
                random_state=42,
            ),
            x_train,
            y_train,
            x_valid,
            y_valid,
            baseline_valid,
        ),
        score(
            "RandomForest 200 trees, min_leaf=20",
            RandomForestRegressor(
                n_estimators=200, min_samples_leaf=20, random_state=42, n_jobs=-1
            ),
            x_train,
            y_train,
            x_valid,
            y_valid,
            baseline_valid,
        ),
        score(
            "Ridge regression",
            Ridge(alpha=1.0),
            x_train,
            y_train,
            x_valid,
            y_valid,
            baseline_valid,
        ),
    ]

    jitter = sum(abs(value - 1.0) for value in y_valid) / len(y_valid)
    print(f"\nirreducible jitter (|observed multiplier - 1.0|): {jitter:.5f}")

    output = {
        "rows": len(features),
        "features": len(features[0]),
        "train_rows": len(x_train),
        "validation_rows": len(x_valid),
        "multiplier_deviation_mae": jitter,
        "configurations": results,
    }

    path = os.environ.get("MODEL_EXPERIMENT_OUTPUT", "/tmp/model_experiment.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
    print(f"\nwritten to {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
