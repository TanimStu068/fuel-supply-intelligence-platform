#!/usr/bin/env python3
"""Measure the CPU cost of one request, isolating it from the background.

The container's CPU counter includes the background sync service and the SSE
reader, so a naive "CPU during the test minus CPU while idle" is wrong: the
background load is not constant, and a busy simulator makes it grow.

The **slope** is the honest measurement. The same endpoint is driven at two
different request rates, and the CPU cost *per request* is the difference
between the two, divided by the difference in the request counts. The
background load cancels out of the subtraction exactly, because it was
running at both rates.

The two rates come from running **1 worker** against the endpoint — which
is limited by the round-trip latency — and **8 workers**, which push the
server harder. Both windows are measured the same way.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx

BASE_URL = "http://localhost:8001"
WINDOW_SECONDS = 6.0

ENDPOINTS = [
    "/",
    "/health",
    "/api/stations",
    "/api/depots",
    "/api/routes",
    "/api/risk",
    "/api/alerts",
    "/api/recommendations?limit=50",
    "/api/events",
    "/api/supply-arrivals",
    "/api/allocations",
    "/api/service-health",
]


def _cgroup() -> Path:
    pid = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Pid}}", "fuel-backend"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[2].startswith("/"):
            candidate = Path("/sys/fs/cgroup") / parts[2].lstrip("/")
            if candidate.exists():
                return candidate
    raise RuntimeError("the backend's cgroup could not be found")


CGROUP = _cgroup()


def _cpu_usec() -> int:
    for line in (CGROUP / "cpu.stat").read_text().splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    raise RuntimeError("cpu.stat has no usage_usec")


async def _drive(
    client: httpx.AsyncClient, endpoint: str, workers: int
) -> tuple[int, int, int]:
    """Drive one endpoint with ``workers`` concurrent workers.

    Returns (requests, cpu_usec, payload_bytes).
    """
    counter = {"n": 0, "bytes": 0}

    async def worker() -> None:
        while True:
            response = await client.get(endpoint)
            counter["n"] += 1
            counter["bytes"] += len(response.content)

    cpu_before = _cpu_usec()
    tasks = [asyncio.create_task(worker()) for _ in range(workers)]
    await asyncio.sleep(WINDOW_SECONDS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    return counter["n"], _cpu_usec() - cpu_before, counter["bytes"]


async def main() -> None:
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=100)
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=httpx.Timeout(10.0), limits=limits
    ) as client:
        print(
            f"{'endpoint':32s} {'rps':>8s} {'us/req':>9s} "
            f"{'KiB/req':>9s} {'status':>7s}"
        )
        print("-" * 72)
        for endpoint in ENDPOINTS:
            probe = await client.get(endpoint)
            status = probe.status_code
            low_n, low_cpu, _ = await _drive(client, endpoint, 1)
            high_n, high_cpu, high_bytes = await _drive(client, endpoint, 8)

            extra_requests = high_n - low_n
            extra_cpu = high_cpu - low_cpu
            us_per_request = extra_cpu / extra_requests if extra_requests else 0.0
            rps = high_n / WINDOW_SECONDS
            kib = high_bytes / high_n / 1024 if high_n else 0.0
            print(
                f"{endpoint:32s} {rps:8.1f} {us_per_request:9.1f} "
                f"{kib:9.1f} {status:7d}"
            )


asyncio.run(main())
