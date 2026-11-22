#!/usr/bin/env python3
"""Day 4 evaluation: compare the decision policy against a naive baseline.

Runs the simulator twice over the same number of ticks — once with the
platform's **priority-greedy decision policy** driving the allocations,
and once with a **naive baseline** — and reports the `service_level` the
simulator measured for each.

## The naive baseline

The baseline is deliberately simple and deliberately *reasonable*, because a
comparison against a strawman proves nothing. It:

* ships to the station with the **lowest inventory** for that fuel,
  regardless of how soon it runs dry;
* always uses the **fastest route**, even when that route is disrupted
  (which is exactly the mistake a naive policy makes);
* ships a **fixed 5,000 L**, ignoring the tank's remaining room.

Everything else — the simulator, the demand, the tick pacing — is
identical between the two runs.

## The measurement

`service_level` comes from `GET /v1/metrics` and is the simulator's own
measure: `served / (served + unmet)`. It cannot be gamed by the platform,
which makes it the honest metric to compare policies on.

Note that the simulator's total supply is about 25% below its total
demand, so service level cannot approach 1.0 in either run. The
comparison is between the two policies, not against a perfect score.

Usage:
    python scripts/day4_policy_comparison.py --ticks 300 --step-pause 1.1

Environment:
    SIMULATOR_URL   default http://localhost:8100
    BACKEND_URL     default http://localhost:8001
    DAY4_OUTPUT     where to write the raw JSON summary
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

#: The naive policy's fixed shipment size.
NAIVE_SHIPMENT_LITERS = 5_000.0

#: How many ticks pass between two platform sync cycles. The naive
#: baseline is held to the same cadence so the comparison measures the
#: quality of the choice, not how often the policy got to act.
PLATFORM_SYNC_INTERVAL_TICKS = 4


def say(message: str) -> None:
    print(f"\n\033[1m{message}\033[0m", flush=True)


def info(message: str) -> None:
    print(f"  {message}", flush=True)


def current_tick(sim: httpx.Client) -> int:
    """The simulator's current tick."""
    return int(sim.get(f"{SIMULATOR_URL}/v1/instance").json()["tick"])


def simulator_metrics(sim: httpx.Client) -> dict[str, Any]:
    """The simulator's own service level and allocation statistics."""
    return sim.get(f"{SIMULATOR_URL}/v1/metrics").json()


def step_paced(sim: httpx.Client, count: int, pause: float) -> None:
    """Advance the simulator by ``count`` ticks, pausing between them.

    The platform is a closed loop: it reads the world, assesses the risk
    and ships fuel. Stepping the simulator as fast as it will go lets the
    world run away from the platform — the backend syncs once a second and
    the simulator ticks thirty times a second, so most ticks are never
    seen by anything that could act on them. The service level of such a
    run measures the step rate, not the policy.
    """
    for _ in range(count):
        sim.post(f"{SIMULATOR_URL}/admin/step").raise_for_status()
        if pause > 0:
            time.sleep(pause)


def wait_for_backend(api: httpx.Client, timeout: float = 60.0) -> bool:
    """Wait until the backend is serving state again."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if api.get(f"{BACKEND_URL}/health").status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False


def wait_for_tick(api: httpx.Client, target: int, timeout: float = 120.0) -> int:
    """Wait until the backend has caught up with the simulator's tick."""
    deadline = time.time() + timeout
    seen = 0
    while time.time() < deadline:
        try:
            response = api.get(f"{BACKEND_URL}/api/state")
            if response.status_code == 200:
                payload = response.json()
                seen = int(payload.get("instance", {}).get("tick", 0) or 0)
                if seen >= target:
                    return seen
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return seen


def approve_recommendations(api: httpx.Client) -> dict[str, int]:
    """Approve every pending recommendation, as the operator would.

    This is the human-in-the-loop step. The platform proposes; the
    operator approves; the simulator receives the shipment.
    """
    response = api.get(f"{BACKEND_URL}/api/recommendations", params={"status": "PROPOSED"})
    if response.status_code != 200:
        return {"approved": 0, "failed": 0}

    approved = 0
    failed = 0
    for recommendation in response.json().get("recommendations", []):
        result = api.post(
            f"{BACKEND_URL}/api/recommendations/{recommendation['id']}/approve",
            json={"decided_by": "policy-comparison", "submit": True},
        )
        if result.status_code == 200 and result.json().get("status") == "SUBMITTED":
            approved += 1
        else:
            failed += 1
    return {"approved": approved, "failed": failed}


def naive_step(sim: httpx.Client, allocations: list[dict[str, Any]]) -> dict[str, Any]:
    """One tick of the naive baseline policy.

    The naive policy reads the simulator's own state directly and ships
    fuel with its own reasoning, so it does not depend on the platform's
    intelligence layer being up. It is what "no intelligence, just move
    fuel" looks like.

    ``allocations`` is the ledger the policy already has, so the
    idempotency key it builds is unique per (station, tick) and a retry
    cannot become a second shipment.
    """
    stations = sim.get(f"{SIMULATOR_URL}/v1/stations").json()
    routes = sim.get(f"{SIMULATOR_URL}/v1/routes").json()
    depots = sim.get(f"{SIMULATOR_URL}/v1/depots").json()
    tick = current_tick(sim)

    depot_by_id = {depot["id"]: depot for depot in depots}

    # The station with the lowest DIESEL inventory, whether or not it is
    # about to run dry.
    candidates = [
        station
        for station in stations
        if station.get("status") == "OPEN"
        and station.get("inventory", {}).get("DIESEL", 0) > 0
    ]
    if not candidates:
        return {"shipped": 0}

    target = min(candidates, key=lambda station: station["inventory"]["DIESEL"])

    # The fastest route to it, even when that route is disrupted.
    candidate_routes = [
        route for route in routes if route["destination_station_id"] == target["id"]
    ]
    if not candidate_routes:
        return {"shipped": 0}

    route = min(candidate_routes, key=lambda item: item["transit_ticks"])
    depot = depot_by_id.get(route["source_depot_id"])
    if depot is None or depot.get("status") not in ("OPEN", "CONSTRAINED"):
        return {"shipped": 0}

    stock = depot.get("inventory", {}).get("DIESEL", 0.0)
    if stock < NAIVE_SHIPMENT_LITERS:
        return {"shipped": 0}

    # One shipment per station per tick, so the naive policy does not
    # out-ship the platform purely by acting more often.
    if any(
        row["destination_station_id"] == target["id"] and row["created_tick"] == tick
        for row in allocations
    ):
        return {"shipped": 0}

    body = {
        "idempotency_key": f"naive-{target['id']}-DIESEL-{tick}",
        "source_depot_id": depot["id"],
        "destination_station_id": target["id"],
        "route_id": route["id"],
        "fuel_type": "DIESEL",
        "quantity": NAIVE_SHIPMENT_LITERS,
    }
    response = sim.post(f"{SIMULATOR_URL}/v1/allocations", json=body)
    if response.status_code >= 400:
        return {"shipped": 0, "error": response.json()}
    return {"shipped": 1}


def reset_world(sim: httpx.Client) -> None:
    """Wipe the simulator's world and restart the clock at tick 0."""
    sim.post(f"{SIMULATOR_URL}/admin/reset").raise_for_status()


def run_policy_run(
    *,
    api: httpx.Client,
    sim: httpx.Client,
    ticks: int,
    step_pause: float,
    policy: str,
) -> dict[str, Any]:
    """Run one policy over the simulator and report what happened.

    Both policies act at the **same cadence** — once per platform sync
    cycle. That matters: the naive policy reads the simulator directly, so
    if it acted on every tick it would simply act more often than the
    platform, and the comparison would measure how often you ship rather
    than how well you choose what to ship.
    """
    reset_world(sim)
    wait_for_backend(api)

    shipped = 0
    approvals = 0
    failures = 0
    last_acted_tick = -1

    for _ in range(ticks):
        step_paced(sim, 1, step_pause)
        tick = current_tick(sim)

        if policy == "platform":
            target = current_tick(sim)
            wait_for_tick(api, target)
            outcome = approve_recommendations(api)
            approvals += outcome["approved"]
            failures += outcome["failed"]
        else:
            # The naive policy acts on the same cadence the platform's
            # sync loop does: once every platform_sync_interval ticks.
            if tick - last_acted_tick >= PLATFORM_SYNC_INTERVAL_TICKS:
                allocations = sim.get(f"{SIMULATOR_URL}/v1/allocations").json()
                outcome = naive_step(sim, allocations)
                shipped += outcome.get("shipped", 0)
                last_acted_tick = tick

    metrics = simulator_metrics(sim)
    allocations = sim.get(f"{SIMULATOR_URL}/v1/allocations").json()
    risks = api.get(f"{BACKEND_URL}/api/risk")
    risk_rows = risks.json().get("risks", []) if risks.status_code == 200 else []

    return {
        "policy": policy,
        "ticks": ticks,
        "service_level": metrics.get("service_level"),
        "served_demand_liters": metrics.get("served_demand_liters"),
        "unmet_demand_liters": metrics.get("unmet_demand_liters"),
        "allocation_liters": metrics.get("allocation_liters"),
        "allocation_failures": metrics.get("allocation_failures"),
        "allocations_created": len(allocations),
        "approvals": approvals,
        "approval_failures": failures,
        "naive_shipments": shipped,
        "risk_rows": len(risk_rows),
        "critical_risks": sum(
            1 for row in risk_rows if row.get("risk_level") == "CRITICAL"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=int, default=300, help="Ticks per run.")
    parser.add_argument(
        "--step-pause",
        type=float,
        default=1.1,
        help="Seconds to wait between simulator steps.",
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("DAY4_OUTPUT", "/tmp/day4_policy_comparison.json"),
        help="Where to write the raw JSON summary.",
    )
    args = parser.parse_args()

    with httpx.Client(timeout=60.0) as api, httpx.Client(timeout=60.0) as sim:
        say("=== 1. Warming up the platform ===")
        if not wait_for_backend(api):
            info("the backend is not reachable")
            return 1
        info(f"backend healthy, simulator at tick {current_tick(sim)}")

        say("=== 2. Running the platform's decision policy ===")
        platform = run_policy_run(
            api=api,
            sim=sim,
            ticks=args.ticks,
            step_pause=args.step_pause,
            policy="platform",
        )
        info(f"service_level = {platform['service_level']}")
        info(f"approvals = {platform['approvals']}, failures = {platform['approval_failures']}")

        say("=== 3. Running the naive baseline ===")
        naive = run_policy_run(
            api=api,
            sim=sim,
            ticks=args.ticks,
            step_pause=args.step_pause,
            policy="naive",
        )
        info(f"service_level = {naive['service_level']}")
        info(f"naive shipments = {naive['naive_shipments']}")

        say("=== 4. Comparing ===")
        platform_level = float(platform["service_level"] or 0.0)
        naive_level = float(naive["service_level"] or 0.0)
        delta = platform_level - naive_level

        print(f"  platform policy : {platform_level:.6f}")
        print(f"  naive baseline  : {naive_level:.6f}")
        print(f"  difference      : {delta:+.6f} ({delta / naive_level * 100:+.2f}%)")

        summary = {
            "ticks": args.ticks,
            "step_pause": args.step_pause,
            "platform": platform,
            "naive": naive,
            "difference": delta,
        }
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, default=str)
        info(f"summary written to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
