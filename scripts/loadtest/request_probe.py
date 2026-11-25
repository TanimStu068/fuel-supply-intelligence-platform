#!/usr/bin/env python3
"""Measure the per-request work the read path does on cached state.

The state cache holds one snapshot, replaced wholesale on every sync. Every
read path re-derives the same JSON from it on every request, and the
metrics middleware re-reads the cgroup files on every request. This
measures both, on the live data, inside the container.

Run it with ``docker exec -i fuel-backend python - < request_probe.py``.
"""

from __future__ import annotations

import json
import statistics
import time
import urllib.request

from app.metrics import publish_container_memory
from app.state_cache import CachedState
from app.simulator_models import (
    Allocation,
    Depot,
    Instance,
    Metrics,
    Region,
    Route,
    SimulatorEvent,
    Station,
    SupplyArrival,
)


def fetch(path: str) -> dict:
    with urllib.request.urlopen(f"http://localhost:8000{path}", timeout=10) as response:
        return json.loads(response.read())


def build_state(payload: dict) -> CachedState:
    """Rebuild the cached state from the API's own JSON payload."""
    return CachedState(
        instance=Instance.model_validate(payload["instance"]),
        regions=[Region.model_validate(r) for r in payload["regions"]],
        depots=[Depot.model_validate(d) for d in payload["depots"]],
        stations=[Station.model_validate(s) for s in payload["stations"]],
        routes=[Route.model_validate(r) for r in payload["routes"]],
        supply_arrivals=[
            SupplyArrival.model_validate(a) for a in payload["supply_arrivals"]
        ],
        events=[SimulatorEvent.model_validate(e) for e in payload["events"]],
        allocations=[Allocation.model_validate(a) for a in payload["allocations"]],
        metrics=Metrics.model_validate(payload["metrics"]),
    )


def measure(label: str, count: int, work) -> None:
    # Warm up.
    for _ in range(5):
        work()
    samples = []
    for _ in range(count):
        started = time.perf_counter()
        work()
        samples.append((time.perf_counter() - started) * 1000)
    print(
        f"{label:52s} {statistics.fmean(samples):8.3f} ms  "
        f"median {statistics.median(samples):8.3f} ms"
    )


def main() -> None:
    payload = fetch("/api/state")
    state = build_state(payload)

    print(
        f"live state: {len(state.stations)} stations, {len(state.allocations)} "
        f"allocations, {len(state.supply_arrivals)} arrivals, "
        f"{len(state.routes)} routes\n"
    )

    measure("to_payload() — the whole state, per request", 200, state.to_payload)
    measure(
        "stations model_dump — /api/stations",
        500,
        lambda: [s.model_dump(mode="json") for s in state.stations],
    )
    measure(
        "allocations model_dump — /api/allocations",
        200,
        lambda: [a.model_dump(mode="json") for a in state.allocations],
    )
    measure(
        "supply_arrivals model_dump — /api/supply-arrivals",
        500,
        lambda: [a.model_dump(mode="json") for a in state.supply_arrivals],
    )
    measure("publish_container_memory() — per request", 500, publish_container_memory)
    measure(
        "json.dumps(to_payload()) — per request",
        100,
        lambda: json.dumps(state.to_payload()),
    )


if __name__ == "__main__":
    main()
