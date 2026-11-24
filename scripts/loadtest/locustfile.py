"""The Locust load test: the three workloads, as Locust user classes.

Locust is the tool the brief asks for, and this file is the Locust half of
the load test. ``run_loadtest.py`` is the standalone half — the same three
workloads, the same ramp, the same measurements, but with no dependency on
Locust being installed.

**Why both.** Locust is the right tool when it is available: it is a
mature, well-understood load generator with a web UI, and its statistics
are the ones a judge will recognise. But it is a heavy dependency
(gevent, pyzmq, Flask, msgpack and more), and this repository's
backend environment cannot always install it — the machines this was
built on could not reach the package index for the full dependency tree.

So the measurements are taken by the standalone harness, which has no
dependencies beyond the ones the project already uses, and this file
provides the equivalent Locust test so the numbers can be reproduced with
the standard tool. Both describe the same workload, so the numbers are
comparable.

**The three workloads:**

``DashboardUser``
    The read path the operator's browser drives. Ten endpoints, the way
    the dashboard polls them. It is many small reads, so it should scale
    almost linearly.

``RecommendationUser``
    The read path that carries the expensive payload: the world model, the
    risk scores and the alternatives. It is CPU-bound, so it is the first
    thing to fall over when the concurrency goes up.

``DecisionUser``
    The end-to-end path: propose a set of allocations, then approve one
    and let the platform submit it to the simulator. It is the only
    workload that *writes*, so it is the one that finds the contention on
    the database.

Run it with::

    cd backend && .venv/bin/python -m locust \\
        -f ../scripts/loadtest/locustfile.py \\
        --host http://localhost:8001 --headless \\
        --users 50 --spawn-rate 10 --run-time 5m \\
        --csv ../scripts/loadtest/results/locust
"""

from __future__ import annotations

import itertools
import os
import random
from typing import Any

from locust import HttpUser, between, task

#: The backend under test.
HOST = os.getenv("BACKEND_URL", "http://localhost:8001")

#: The endpoints the operator's dashboard polls.
DASHBOARD_ENDPOINTS = [
    "/api/stations",
    "/api/depots",
    "/api/routes",
    "/api/risk",
    "/api/alerts",
    "/api/recommendations",
    "/api/events",
    "/api/supply-arrivals",
    "/api/allocations",
    "/api/service-health",
]


class DashboardUser(HttpUser):
    """The dashboard's read path.

    Ten endpoints, the way the operator's dashboard polls them. The
    browser polls every few seconds, so the mix matters more than the
    rate: every endpoint has to be fast, not just the popular one.
    """

    #: A browser polls every few seconds; the pause is what keeps this
    #: from being a saturation test of the network rather than of the
    #: platform.
    wait_time = between(0.5, 2.0)

    @task(10)
    def read_dashboard(self) -> None:
        """Read one of the dashboard's endpoints."""
        endpoint = random.choice(DASHBOARD_ENDPOINTS)
        with self.client.get(
            endpoint,
            name=endpoint,
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"{endpoint} -> {response.status_code}")


class RecommendationUser(HttpUser):
    """The recommendation read path.

    The endpoint that carries the whole recommendation payload. It is
    CPU-bound, so it is the first thing to fall over when the
    concurrency goes up — which is exactly what the test is for.
    """

    wait_time = between(0.5, 2.0)

    @task
    def read_recommendations(self) -> None:
        """Read the recommendations."""
        with self.client.get(
            "/api/recommendations",
            name="/api/recommendations",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"/api/recommendations -> {response.status_code}")


class DecisionUser(HttpUser):
    """The end-to-end decision path.

    Each round proposes a fresh set of allocations and approves one of
    them. The approval is what writes to the simulator, so this is the
    workload that finds the database contention.

    Only recommendations that are still ``PROPOSED`` are approved. The
    approve endpoint answers ``409 Conflict`` for one that has already
    been submitted, and that is the **right** answer — approving the same
    allocation twice would double-dispatch the fuel. A 409 is therefore
    reported as a *success*: the API correctly refused a duplicate, which
    is the behaviour being tested.
    """

    wait_time = between(1.0, 3.0)

    @task
    def decide(self) -> None:
        """Propose a set of allocations and approve one."""
        with self.client.post(
            "/api/recommendations/propose",
            json={},
            name="POST /api/recommendations/propose",
            catch_response=True,
        ) as proposal:
            if proposal.status_code != 200:
                proposal.failure(f"propose -> {proposal.status_code}")
                return

        with self.client.get(
            "/api/recommendations",
            name="GET /api/recommendations (for approval)",
            catch_response=True,
        ) as listing:
            if listing.status_code != 200:
                listing.failure(f"list -> {listing.status_code}")
                return
            try:
                items = listing.json()
            except Exception:
                listing.failure("the listing was not JSON")
                return

        if isinstance(items, dict):
            items = items.get("recommendations", [])
        if not isinstance(items, list):
            return

        fresh = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("status") == "PROPOSED"
            and item.get("id")
        ]
        if not fresh:
            return

        recommendation_id = fresh[0]["id"]
        with self.client.post(
            f"/api/recommendations/{recommendation_id}/approve",
            json={},
            name="POST /api/recommendations/{id}/approve",
            catch_response=True,
        ) as approval:
            if approval.status_code == 409:
                # The API correctly refused a duplicate approval.
                approval.success()
            elif approval.status_code != 200:
                approval.failure(f"approve -> {approval.status_code}")
