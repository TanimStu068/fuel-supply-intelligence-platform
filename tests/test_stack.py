"""Cross-service checks against a running stack.

These are *not* part of the fast unit suite — they need the docker-compose
stack to be up. Run them with:

    pytest tests/ -m integration

or directly:

    pytest tests/test_stack.py
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from conftest import BACKEND_URL, SIMULATOR_URL

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def simulator_url() -> str:
    return SIMULATOR_URL


@pytest.fixture(scope="module")
def backend_url() -> str:
    return BACKEND_URL


@pytest.fixture(scope="module")
def http_client() -> httpx.Client:
    with httpx.Client(timeout=15.0) as client:
        yield client


def test_simulator_is_reachable(http_client: httpx.Client, simulator_url: str) -> None:
    response = http_client.get(f"{simulator_url}/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_backend_health_is_green(http_client: httpx.Client, backend_url: str) -> None:
    response = http_client.get(f"{backend_url}/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["components"]["backend"]["status"] == "ok"
    assert body["components"]["database"]["status"] == "ok"
    assert body["components"]["simulator"]["status"] == "ok"


def test_backend_state_has_every_section(
    http_client: httpx.Client, backend_url: str
) -> None:
    response = http_client.get(f"{backend_url}/api/state")
    assert response.status_code == 200

    body = response.json()
    for key in (
        "instance",
        "regions",
        "depots",
        "stations",
        "routes",
        "supply_arrivals",
        "events",
        "allocations",
        "metrics",
        "stale",
        "degraded",
        "age_seconds",
    ):
        assert key in body, f"missing section: {key}"

    assert len(body["depots"]) == 2
    assert len(body["stations"]) == 4
    assert len(body["routes"]) == 6
    assert body["metrics"]["service_level"] >= 0.0


@pytest.mark.parametrize(
    ("path", "key"),
    [
        ("/api/stations", "stations"),
        ("/api/depots", "depots"),
        ("/api/routes", "routes"),
        ("/api/events", "events"),
        ("/api/supply-arrivals", "supply_arrivals"),
        ("/api/allocations", "allocations"),
    ],
)
def test_read_endpoints_serve_the_stored_state(
    http_client: httpx.Client, backend_url: str, path: str, key: str
) -> None:
    response = http_client.get(f"{backend_url}{path}")
    assert response.status_code == 200

    body = response.json()
    assert key in body
    assert body["degraded"] is False
    assert body["stale"] is False


def test_history_endpoint_serves_stored_demand(
    http_client: httpx.Client, backend_url: str
) -> None:
    response = http_client.get(f"{backend_url}/api/history/station-mirpur")
    assert response.status_code == 200

    body = response.json()
    assert body["station_id"] == "station-mirpur"
    assert "observations" in body


def test_metrics_expose_the_sync_series(
    http_client: httpx.Client, backend_url: str
) -> None:
    response = http_client.get(f"{backend_url}/metrics")
    assert response.status_code == 200

    text = response.text
    for series in (
        "http_requests_total",
        "http_request_duration_seconds",
        "simulator_requests_total",
        "sse_reconnects_total",
        "last_sync_age_seconds",
        "degraded_mode",
        "stale_data",
    ):
        assert series in text, f"missing metric: {series}"


def test_backend_exposes_prometheus_metrics(
    http_client: httpx.Client, backend_url: str
) -> None:
    response = http_client.get(f"{backend_url}/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text


def test_allocation_round_trip(http_client: httpx.Client, simulator_url: str) -> None:
    """Create, replay, and cancel an allocation end to end."""
    # A cancelled allocation permanently occupies its idempotency key, so each
    # run needs a fresh key to reach the CANCELLED branch.
    body = {
        "idempotency_key": f"e2e-round-trip-{uuid.uuid4().hex[:12]}",
        "source_depot_id": "depot-gazipur",
        "destination_station_id": "station-mirpur",
        "route_id": "route-gazipur-mirpur",
        "fuel_type": "DIESEL",
        "quantity": 500,
    }

    created = http_client.post(f"{simulator_url}/v1/allocations", json=body)
    assert created.status_code in (200, 201)
    allocation_id = created.json()["id"]

    replay = http_client.post(f"{simulator_url}/v1/allocations", json=body)
    assert replay.status_code in (200, 201)
    assert replay.json()["id"] == allocation_id

    cancelled = http_client.post(
        f"{simulator_url}/v1/allocations/{allocation_id}/cancel"
    )
    assert cancelled.status_code in (200, 201)
    assert cancelled.json()["status"] == "CANCELLED"


def test_unknown_depot_returns_404(
    http_client: httpx.Client, simulator_url: str
) -> None:
    response = http_client.get(f"{simulator_url}/v1/depots/depot-does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "NOT_FOUND"
