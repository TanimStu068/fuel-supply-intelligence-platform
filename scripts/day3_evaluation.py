#!/usr/bin/env python3
"""Day 3 evaluation: speed the simulator up, train, evaluate, spike.

Drives the simulator through `/admin/step`, waits for the backend to ingest
and score, then measures how fast a demand spike shows up in the risk
assessment. Everything it prints is written to `docs/model-evaluation.md`.

The spike is scheduled a few ticks *ahead* of the current tick, so the
simulator actually runs it: an event whose `start_tick` is already in the
past starts and resolves within the same tick and is never observable.

Usage:
    python scripts/day3_evaluation.py --ticks 400 --watch-ticks 60

Environment:
    SIMULATOR_URL   default http://localhost:8100
    BACKEND_URL     default http://localhost:8001
    DAY3_OUTPUT     where to write the raw JSON summary
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


def step(sim: httpx.Client, count: int) -> None:
    """Advance the simulator by ``count`` ticks."""
    for _ in range(count):
        sim.post(f"{SIMULATOR_URL}/admin/step").raise_for_status()


def step_paced(sim: httpx.Client, count: int, pause: float) -> None:
    """Advance the simulator by ``count`` ticks, pausing between them.

    The platform is a closed loop: it reads the world, assesses the risk
    and ships fuel. Stepping the simulator as fast as it will go lets the
    world run away from the platform — the backend syncs once a second and
    the simulator ticks thirty times a second, so most ticks are never
    seen by anything that could act on them. The service level of such a
    run measures the step rate, not the policy.

    Pacing the steps to the platform's own cycle is what makes the
    comparison honest: every tick is one the platform had a chance to
    respond to.
    """
    for _ in range(count):
        sim.post(f"{SIMULATOR_URL}/admin/step").raise_for_status()
        if pause > 0:
            time.sleep(pause)


def current_tick(sim: httpx.Client) -> int:
    return int(sim.get(f"{SIMULATOR_URL}/v1/instance").json()["tick"])


def wait_for_ingest(api: httpx.Client, expected_rows: int, timeout: float = 120.0) -> int:
    """Wait until the backend has stored at least ``expected_rows`` observations."""
    deadline = time.time() + timeout
    rows = 0
    while time.time() < deadline:
        response = api.get(f"{BACKEND_URL}/api/history/station-mirpur")
        if response.status_code == 200:
            rows = int(response.json().get("count", 0))
            if rows >= expected_rows:
                return rows
        time.sleep(2.0)
    return rows


def risk_for(api: httpx.Client, station_id: str, fuel: str) -> dict[str, Any] | None:
    response = api.get(
        f"{BACKEND_URL}/api/risk",
        params={"station_id": station_id, "fuel_type": fuel},
    )
    if response.status_code != 200:
        return None
    risks = response.json().get("risks", [])
    return risks[0] if risks else None


def anomalies_for(api: httpx.Client, station_id: str) -> list[dict[str, Any]]:
    response = api.get(f"{BACKEND_URL}/api/anomalies")
    if response.status_code != 200:
        return []
    return [
        anomaly
        for anomaly in response.json().get("anomalies", [])
        if anomaly.get("entity_id") == station_id
    ]


def metrics_snapshot(api: httpx.Client) -> dict[str, str]:
    """The intelligence metrics worth reporting."""
    response = api.get(f"{BACKEND_URL}/metrics")
    if response.status_code != 200:
        return {}
    wanted = (
        "prediction_error_mae_liters",
        "prediction_interval_coverage_ratio",
        "model_confidence",
        "fallback_activations_total",
        "risk_assessments_total",
        "anomalies_detected_total",
    )
    lines = []
    for line in response.text.splitlines():
        if any(line.startswith(name) for name in wanted):
            lines.append(line)
    return {"raw": "\n".join(lines)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=int, default=400)
    parser.add_argument("--watch-ticks", type=int, default=60)
    parser.add_argument("--spike-multiplier", type=float, default=2.0)
    parser.add_argument("--station", default="station-mirpur")
    parser.add_argument("--fuel", default="DIESEL")
    parser.add_argument("--poll-seconds", type=float, default=6.0)
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=30.0,
        help="how long to wait for the backend to publish this tick's anomaly",
    )
    parser.add_argument(
        "--step-pause",
        type=float,
        default=0.0,
        help=(
            "seconds to wait between simulator steps. 0 steps as fast as the "
            "simulator allows, which lets the world run away from the "
            "platform; a non-zero value paces the world to the platform's "
            "own sync cycle so every tick is one the platform could act on"
        ),
    )
    args = parser.parse_args()

    sim = client()
    api = client()

    say("=== 0. Resetting the simulator ===")
    sim.post(f"{SIMULATOR_URL}/admin/reset").raise_for_status()
    info("simulator reset to tick 0")

    say(
        f"=== 1. Generating {args.ticks} ticks "
        f"(~{args.ticks // TICKS_PER_DAY} simulated days) ==="
    )
    started = time.time()
    remaining = args.ticks
    while remaining > 0:
        chunk = min(96, remaining)
        step_paced(sim, chunk, args.step_pause)
        remaining -= chunk
        info(f"tick {current_tick(sim)} ({time.time() - started:.1f}s)")
    info(f"reached tick {current_tick(sim)} in {time.time() - started:.1f}s")

    say("=== 2. Waiting for the backend to ingest the history ===")
    rows = wait_for_ingest(api, args.ticks * 3)
    info(f"demand observations stored: {rows}")

    say("=== 3. Training the learned model ===")
    trained = api.post(f"{BACKEND_URL}/api/model/train").json()
    info(json.dumps(trained, indent=2))

    status = api.get(f"{BACKEND_URL}/api/model-status").json()
    info("model status:")
    info(json.dumps(status, indent=2))

    say("=== 4. Risk before the spike ===")
    before = api.get(f"{BACKEND_URL}/api/risk").json()
    info(json.dumps(before.get("summary", {}), indent=2))
    info(f"active model: {before.get('active_model')}")
    info(f"models used: {before.get('models_used')}")

    # The event must start *after* the current tick, or the simulator
    # starts and resolves it within a single tick and it is never visible.
    spike_tick = current_tick(sim) + 3
    say(
        f"=== 5. Injecting a demand spike (x{args.spike_multiplier}) "
        f"at tick {spike_tick} ==="
    )
    event = sim.post(
        f"{SIMULATOR_URL}/admin/events",
        json={
            "type": "demand_spike",
            "start_tick": spike_tick,
            "duration_ticks": 48,
            "parameters": {
                "multiplier": args.spike_multiplier,
                "station_ids": [args.station],
            },
        },
    )
    info(json.dumps(event.json(), indent=2))

    say("=== 6. Watching the risk react ===")
    print(f"{'tick':<7}{'risk':<11}{'hours':<8}{'prob':<8}{'anom':<6}{'model':<10}{'disp'}")
    timeline: list[dict[str, Any]] = []
    first_anomaly_tick: int | None = None
    first_risk_change_tick: int | None = None
    pre_spike_risk = risk_for(api, args.station, args.fuel)
    pre_spike_level = pre_spike_risk["risk_level"] if pre_spike_risk else "OK"

    for _ in range(args.watch_ticks):
        step(sim, 1)
        tick = current_tick(sim)

        # The backend polls the simulator every few seconds, so the
        # anomaly for this tick is not visible until it has run a cycle.
        # Waiting for the *anomaly* to appear measures the pipeline's
        # latency, not a fixed sleep.
        anomalies = anomalies_for(api, args.station)
        deadline = time.time() + args.settle_seconds
        while not anomalies and time.time() < deadline:
            time.sleep(1.0)
            anomalies = anomalies_for(api, args.station)

        risk = risk_for(api, args.station, args.fuel)
        if risk is None:
            continue

        level = risk["risk_level"]
        hours = risk.get("hours_to_stockout")
        probability = risk.get("stockout_probability")
        model = risk.get("model")

        decisions = api.get(f"{BACKEND_URL}/api/decisions").json()
        dispatched = decisions.get("count", 0)

        hours_text = "-" if hours is None else f"{hours:.1f}"
        print(
            f"{tick:<7}{level:<11}{hours_text:<8}{probability:<8}"
            f"{len(anomalies):<6}{model:<10}{dispatched}"
        )

        timeline.append(
            {
                "tick": tick,
                "risk_level": level,
                "hours_to_stockout": hours,
                "stockout_probability": probability,
                "anomaly_count": len(anomalies),
                "anomaly_messages": [a["message"] for a in anomalies],
                "model": model,
                "dispatched": dispatched,
                "needs_human_review": risk.get("needs_human_review"),
            }
        )

        if first_anomaly_tick is None and anomalies:
            first_anomaly_tick = tick
        if first_risk_change_tick is None and level != pre_spike_level:
            first_risk_change_tick = tick

    say("=== 7. Summary ===")
    summary: dict[str, Any] = {
        "pre_spike_risk": pre_spike_risk,
        "first_anomaly_tick": first_anomaly_tick,
        "first_risk_change_tick": first_risk_change_tick,
        "spike_injected_at": spike_tick,
        "anomaly_detection_ticks": (
            first_anomaly_tick - spike_tick if first_anomaly_tick else None
        ),
        "risk_change_ticks": (
            first_risk_change_tick - spike_tick if first_risk_change_tick else None
        ),
        "timeline": timeline,
    }

    after = api.get(f"{BACKEND_URL}/api/risk").json()
    summary["final_summary"] = after.get("summary")
    summary["final_models_used"] = after.get("models_used")
    summary["final_fallback_activations"] = after.get("fallback_activations")
    summary["model_status"] = api.get(f"{BACKEND_URL}/api/model-status").json()
    summary["decisions"] = api.get(f"{BACKEND_URL}/api/decisions").json()
    summary["metrics"] = metrics_snapshot(api)
    summary["simulator_metrics"] = sim.get(f"{SIMULATOR_URL}/v1/metrics").json()

    print(json.dumps(summary, indent=2, default=str))

    output = os.environ.get("DAY3_OUTPUT", "/tmp/day3_evaluation.json")
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, default=str)
    info(f"full summary written to {output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
