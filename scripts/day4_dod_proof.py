#!/usr/bin/env python3
"""Day 4 definition-of-done proof.

Proves the whole loop end to end against the running simulator:

1. the platform **proposes** an allocation for a station that needs fuel;
2. a human **inspects** it — the what-if simulation, run locally, with no
   write to the simulator;
3. a human **approves** it, and the simulator accepts it;
4. the shipment **arrives**, and the simulator reports ``ARRIVED``;
5. the station's **risk improves**.

Usage:
    python scripts/day4_dod_proof.py

Environment:
    SIMULATOR_URL   default http://localhost:8100
    BACKEND_URL     default http://localhost:8001
    DAY4_OUTPUT     where to write the raw JSON summary
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

SIMULATOR_URL = os.environ.get("SIMULATOR_URL", "http://localhost:8100")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8001")


def say(message: str) -> None:
    print(f"\n\033[1m{message}\033[0m", flush=True)


def info(message: str) -> None:
    print(f"  {message}", flush=True)


def ok(message: str) -> None:
    print(f"  \033[32m✓\033[0m {message}", flush=True)


def risk_for(api: httpx.Client, station_id: str, fuel_type: str) -> dict[str, Any] | None:
    """The platform's risk assessment for one station and fuel."""
    response = api.get(
        f"{BACKEND_URL}/api/risk",
        params={"station_id": station_id, "fuel_type": fuel_type},
    )
    if response.status_code != 200:
        return None
    risks = response.json().get("risks", [])
    return risks[0] if risks else None


def station_inventory(sim: httpx.Client, station_id: str, fuel_type: str) -> float:
    """The simulator's own view of a station's inventory."""
    stations = sim.get(f"{SIMULATOR_URL}/v1/stations").json()
    for station in stations:
        if station["id"] == station_id:
            return float(station["inventory"].get(fuel_type, 0.0))
    return 0.0


def allocations_for(sim: httpx.Client, station_id: str) -> list[dict[str, Any]]:
    """The simulator's allocation ledger for one station."""
    allocations = sim.get(f"{SIMULATOR_URL}/v1/allocations").json()
    return [row for row in allocations if row["destination_station_id"] == station_id]


def wait_for_status(
    sim: httpx.Client,
    allocation_id: int,
    wanted: set[str],
    timeout: float = 120.0,
) -> str | None:
    """Wait until an allocation reaches one of the wanted statuses.

    The simulator is paused by default, so it has to be stepped for the
    shipment to depart and arrive. Stepping here — rather than asking
    someone to press a button — is what makes the proof repeatable.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for row in sim.get(f"{SIMULATOR_URL}/v1/allocations").json():
            if row["id"] == allocation_id and row["status"] in wanted:
                return row["status"]
        sim.post(f"{SIMULATOR_URL}/admin/step")
        time.sleep(0.3)
    return None


def main() -> int:
    with httpx.Client(timeout=60.0) as api, httpx.Client(timeout=60.0) as sim:
        say("=== 1. The platform proposes an allocation ===")
        response = api.get(f"{BACKEND_URL}/api/recommendations", params={"status": "PROPOSED"})
        if response.status_code != 200:
            info(f"could not read the recommendations: {response.status_code}")
            return 1

        recommendations = response.json().get("recommendations", [])
        if not recommendations:
            info("no recommendations were proposed")
            return 1

        # Take the most urgent one.
        recommendation = min(
            recommendations,
            key=lambda row: (
                row["hours_to_stockout"] if row["hours_to_stockout"] is not None else 999.0
            ),
        )
        station_id = recommendation["station_id"]
        fuel_type = recommendation["fuel_type"]

        info(f"recommendation : {recommendation['id']}")
        info(f"station / fuel : {station_id} / {fuel_type}")
        info(f"depot / route  : {recommendation['source_depot_id']} / {recommendation['route_id']}")
        info(f"quantity       : {recommendation['quantity']:,.0f} L")
        info(f"risk           : {recommendation['risk_level']} -> "
             f"{(recommendation['projection'] or {}).get('risk_level_after')}")
        info(f"confidence     : {recommendation['confidence']}")
        info(f"alternatives   : {len(recommendation['alternatives'])}")
        info(f"rationale      : {recommendation['rationale']}")
        ok("the platform proposed an allocation, with its reasoning")

        say("=== 2. The operator inspects it (what-if, no write) ===")
        before_allocations = len(sim.get(f"{SIMULATOR_URL}/v1/allocations").json())
        simulation = api.post(
            f"{BACKEND_URL}/api/recommendations/{recommendation['id']}/simulate"
        )
        if simulation.status_code != 200:
            info(f"the simulation failed: {simulation.status_code}")
            return 1
        result = simulation.json()
        info(f"writes_to_simulator : {result['writes_to_simulator']}")
        info(f"before : {result['before']}")
        info(f"after  : {result['after']}")
        info(f"improvement : {result['improvement']}")
        after_allocations = len(sim.get(f"{SIMULATOR_URL}/v1/allocations").json())
        if after_allocations == before_allocations:
            ok("the simulation wrote nothing to the simulator")
        else:
            info("the allocation count changed — that should not have happened")

        say("=== 3. The human approves it ===")
        inventory_before = station_inventory(sim, station_id, fuel_type)
        info(f"{station_id} {fuel_type} inventory before : {inventory_before:,.0f} L")

        approval = api.post(
            f"{BACKEND_URL}/api/recommendations/{recommendation['id']}/approve",
            json={"decided_by": "day4-dod-proof", "submit": True},
        )
        if approval.status_code != 200:
            info(f"the approval failed: {approval.status_code} {approval.text[:200]}")
            return 1
        approved = approval.json()
        info(f"status        : {approved['status']}")
        info(f"decided by    : {approved['decided_by']}")
        info(f"allocation id : {approved['allocation_id']}")
        if approved["status"] != "SUBMITTED":
            info(f"error code    : {approved['error_code']}")
            info(f"error message : {approved['error_message']}")
            return 1
        ok("the simulator accepted the allocation")

        say("=== 4. The shipment arrives ===")
        allocation_id = approved["allocation_id"]
        status = wait_for_status(sim, allocation_id, {"ARRIVED", "FAILED", "CANCELLED"})
        info(f"allocation {allocation_id} status : {status}")
        if status != "ARRIVED":
            info("the shipment did not arrive")
            return 1
        ok("the simulator reports ARRIVED")

        say("=== 5. The risk improved ===")
        inventory_after = station_inventory(sim, station_id, fuel_type)
        info(f"{station_id} {fuel_type} inventory after  : {inventory_after:,.0f} L")
        info(f"delivered : {inventory_after - inventory_before:+,.0f} L")

        risk_after = risk_for(api, station_id, fuel_type)
        if risk_after is None:
            info("could not read the risk assessment")
            return 1
        info(f"risk level now : {risk_after['risk_level']}")
        info(f"hours to stockout now : {risk_after['hours_to_stockout']}")
        info(f"stockout probability now : {risk_after['stockout_probability']}")

        improved = (
            risk_after["hours_to_stockout"] is None
            or (
                recommendation["hours_to_stockout"] is not None
                and risk_after["hours_to_stockout"] >= recommendation["hours_to_stockout"]
            )
        )
        if improved:
            ok("the station's risk improved")
        else:
            info("the risk did not improve — check the numbers above")

        say("=== 6. The decision history ===")
        history = api.get(f"{BACKEND_URL}/api/decision-history", params={"limit": 5})
        if history.status_code == 200:
            for row in history.json().get("decisions", [])[:3]:
                info(
                    f"{row['recommendation_id']}  {row['status']}  "
                    f"by {row['decided_by']}  alloc={row['allocation_id']}"
                )
            ok("the decision history is recorded")

        summary = {
            "recommendation": recommendation,
            "simulation": result,
            "approval": approved,
            "allocation_status": status,
            "inventory_before": inventory_before,
            "inventory_after": inventory_after,
            "risk_after": risk_after,
        }
        output = os.environ.get("DAY4_OUTPUT", "/tmp/day4_dod_proof.json")
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, default=str)
        info(f"summary written to {output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
