#!/usr/bin/env python3
"""Measure how fast a demand spike changes a station's risk level.

The full evaluation runs the simulator for six days, and by the time the
spike is injected the depot supply is spent: every station is at zero
inventory and already CRITICAL. A spike on an empty station cannot move
the risk level, because it is pinned at the bottom.

This runs a shorter, self-contained experiment on a fresh simulator, where
the station still holds stock, and measures the ticks between the spike
and each observable change: the anomaly, the forecast, the risk level and
the dispatch decision.

Usage:
    python scripts/spike_response.py --ticks 120 --spike-ticks 40
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

SIMULATOR_URL = os.environ.get("SIMULATOR_URL", "http://localhost:8100")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8001")
TICKS_PER_DAY = 96


def say(message: str) -> None:
    print(f"\n\033[1m{message}\033[0m", flush=True)


def info(message: str) -> None:
    print(f"  {message}", flush=True)


def client() -> httpx.Client:
    return httpx.Client(timeout=30.0)


def current_tick(sim: httpx.Client) -> int:
    return int(sim.get(f"{SIMULATOR_URL}/v1/instance").json()["tick"])


def risk_for(api: httpx.Client, station: str, fuel: str) -> dict[str, Any]:
    payload = api.get(f"{BACKEND_URL}/api/risk").json()
    for row in payload.get("risks", []):
        if row["station_id"] == station and row["fuel_type"] == fuel:
            return row
    return {}


def anomalies_for(api: httpx.Client, station: str, fuel: str) -> list[dict[str, Any]]:
    payload = api.get(f"{BACKEND_URL}/api/anomalies").json()
    return [
        row
        for row in payload.get("anomalies", [])
        if row.get("station_id") == station and row.get("fuel_type") == fuel
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=int, default=120)
    parser.add_argument("--spike-ticks", type=int, default=40)
    parser.add_argument("--station", default="station-mirpur")
    parser.add_argument("--fuel", default="PETROL")
    parser.add_argument("--multiplier", type=float, default=3.0)
    parser.add_argument("--step-pause", type=float, default=1.1)
    args = parser.parse_args()

    sim = client()
    api = client()

    say("=== 0. Resetting the simulator ===")
    sim.post(f"{SIMULATOR_URL}/admin/reset").raise_for_status()
    info("simulator reset to tick 0")

    say(f"=== 1. Running {args.ticks} ticks to build up state ===")
    started = time.time()
    for _ in range(args.ticks):
        sim.post(f"{SIMULATOR_URL}/admin/step").raise_for_status()
        if args.step_pause > 0:
            time.sleep(args.step_pause)
    info(f"reached tick {current_tick(sim)} in {time.time() - started:.1f}s")

    say("=== 2. Waiting for the backend to score the current tick ===")
    time.sleep(12)

    before = risk_for(api, args.station, args.fuel)
    say(f"=== 3. Risk before the spike: {args.station} / {args.fuel} ===")
    info(json.dumps(
        {
            "risk_level": before.get("risk_level"),
            "hours_to_stockout": before.get("hours_to_stockout"),
            "stockout_probability": before.get("stockout_probability"),
            "signals": {
                s["name"]: s["value"] for s in before.get("signals", [])
            },
        },
        indent=2,
    ))

    spike_at = current_tick(sim) + 2
    say(f"=== 4. Injecting a x{args.multiplier} demand spike at tick {spike_at} ===")
    response = sim.post(
        f"{SIMULATOR_URL}/admin/events",
        json={
            "type": "demand_spike",
            "start_tick": spike_at,
            "duration_ticks": 48,
            "parameters": {
                "multiplier": args.multiplier,
                "station_ids": [args.station],
            },
        },
    )
    response.raise_for_status()
    info(json.dumps(response.json(), indent=2))

    say("=== 5. Watching the risk react ===")
    print("tick   risk       hours   prob    anom  inv      fcst     model", flush=True)

    first_anomaly: int | None = None
    first_risk_change: int | None = None
    first_forecast_change: int | None = None
    first_dispatch: int | None = None
    baseline_forecast: float | None = None
    previous_forecast: float | None = None
    previous_risk: str | None = None
    timeline: list[dict[str, Any]] = []

    for _ in range(40):
        sim.post(f"{SIMULATOR_URL}/admin/step").raise_for_status()
        if args.step_pause > 0:
            time.sleep(args.step_pause)
        time.sleep(2.0)

        tick = current_tick(sim)
        risk = risk_for(api, args.station, args.fuel)
        signals = {s["name"]: s["value"] for s in risk.get("signals", [])}
        anomalies = anomalies_for(api, args.station, args.fuel)
        forecast = signals.get("forecast_demand_liters")

        if baseline_forecast is None and forecast is not None:
            baseline_forecast = forecast
        if previous_forecast is not None and forecast is not None:
            if abs(forecast - previous_forecast) > 0.01 * max(previous_forecast, 1.0):
                if first_forecast_change is None:
                    first_forecast_change = tick
        if forecast is not None:
            previous_forecast = forecast

        if anomalies and first_anomaly is None:
            first_anomaly = tick

        level = risk.get("risk_level")
        if previous_risk is not None and level != previous_risk:
            if first_risk_change is None:
                first_risk_change = tick
        if level is not None:
            previous_risk = level

        decisions = api.get(f"{BACKEND_URL}/api/decisions").json()
        recent = [
            d
            for d in decisions.get("decisions", [])
            if d.get("station_id") == args.station and d.get("fuel_type") == args.fuel
        ]
        if recent and first_dispatch is None:
            first_dispatch = tick

        print(
            f"{tick:<6} {str(level):<10} {str(risk.get('hours_to_stockout')):<7} "
            f"{str(risk.get('stockout_probability')):<7} {len(anomalies):<5} "
            f"{signals.get('inventory_liters', 0):<8.0f} {forecast or 0:<8.0f} "
            f"{risk.get('model', '-')}",
            flush=True,
        )
        timeline.append(
            {
                "tick": tick,
                "risk_level": level,
                "hours_to_stockout": risk.get("hours_to_stockout"),
                "stockout_probability": risk.get("stockout_probability"),
                "anomaly_count": len(anomalies),
                "inventory_liters": signals.get("inventory_liters"),
                "forecast_demand_liters": forecast,
                "model": risk.get("model"),
            }
        )

    say("=== 6. Summary ===")
    summary = {
        "station_id": args.station,
        "fuel_type": args.fuel,
        "spike_injected_at": spike_at,
        "spike_multiplier": args.multiplier,
        "risk_before": before.get("risk_level"),
        "risk_after": timeline[-1]["risk_level"] if timeline else None,
        "first_anomaly_tick": first_anomaly,
        "anomaly_detection_ticks": (
            first_anomaly - spike_at if first_anomaly is not None else None
        ),
        "first_forecast_change_tick": first_forecast_change,
        "forecast_change_ticks": (
            first_forecast_change - spike_at if first_forecast_change is not None else None
        ),
        "first_risk_change_tick": first_risk_change,
        "risk_change_ticks": (
            first_risk_change - spike_at if first_risk_change is not None else None
        ),
        "first_dispatch_tick": first_dispatch,
        "dispatch_ticks": (
            first_dispatch - spike_at if first_dispatch is not None else None
        ),
        "timeline": timeline,
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "timeline"}, indent=2))
    print("\nfull timeline:", flush=True)
    for row in timeline:
        print(json.dumps(row), flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
