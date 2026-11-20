"""Tests for the API endpoints that read from the stored state.

The fake simulator and database stand in for the real ones, so these tests
exercise the routing, the payload shape and — importantly — the degraded
and stale flags that travel with every response.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_cache, get_database, get_simulator, get_sync
from app.state_cache import CachedState, StateCache


class _FakeSimulator:
    """Minimal stand-in for :class:`SimulatorClient`."""

    def __init__(self, stale: bool = False, unreachable: bool = False) -> None:
        self.stale = stale
        self.last_stale = stale
        self.unreachable = unreachable

    def reset_stale(self) -> None:
        self.stale = False
        self.last_stale = False

    async def health(self, *, fast: bool = False):
        from app.simulator_errors import SimulatorConnectionError
        from app.simulator_models import HealthResponse

        if self.unreachable:
            raise SimulatorConnectionError("connection refused")

        return HealthResponse.model_validate(
            {
                "status": "ok",
                "database": "ok",
                "simulation": {"status": "RUNNING", "tick": 3},
            }
        )


class _FakeDatabase:
    async def ping(self) -> bool:
        return True

    def session(self):
        return _FakeSession()


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def execute(self, statement: Any) -> Any:
        return _FakeResult()

    async def commit(self) -> None:
        return None


class _FakeResult:
    def scalars(self):
        return []

    def first(self):
        return None

    def all(self):
        return []


class _FakeSync:
    """A stand-in for the sync service's status block."""

    def status(self) -> dict[str, Any]:
        return {
            "polling": True,
            "poll_interval_seconds": 5.0,
            "last_sync_age_seconds": 1.2,
            "degraded": False,
            "stale": False,
            "has_state": True,
            "checked_at": "2026-01-01T00:00:00+00:00",
        }


def _populated_cache(stale: bool = False, degraded: bool = False) -> StateCache:
    """A cache holding one realistic snapshot."""
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

    cache = StateCache()
    cache.store(
        CachedState(
            instance=Instance.model_validate(
                {
                    "id": 1,
                    "scenario_id": "baseline",
                    "scenario_version": "1.0",
                    "seed": 1,
                    "sim_time": "2026-01-01T00:00:00+00:00",
                    "tick": 3,
                    "tick_minutes": 15,
                    "status": "RUNNING",
                }
            ),
            regions=[
                Region.model_validate({"id": "region-dhaka", "name": "Dhaka", "demand_factor": 1.0})
            ],
            depots=[
                Depot.model_validate(
                    {
                        "id": "depot-gazipur",
                        "name": "Gazipur",
                        "region_id": "region-dhaka",
                        "status": "OPEN",
                        "dispatch_capacity_per_tick": 12000,
                        "capacity": {"DIESEL": 90000},
                        "inventory": {"DIESEL": 60000},
                    }
                )
            ],
            stations=[
                Station.model_validate(
                    {
                        "id": "station-mirpur",
                        "name": "Mirpur",
                        "region_id": "region-dhaka",
                        "status": "OPEN",
                        "demand_profile": "urban_high",
                        "demand_multiplier": 1.0,
                        "capacity": {"DIESEL": 15000},
                        "inventory": {"DIESEL": 9000},
                    }
                )
            ],
            routes=[
                Route.model_validate(
                    {
                        "id": "route-gazipur-mirpur",
                        "source_depot_id": "depot-gazipur",
                        "destination_station_id": "station-mirpur",
                        "transit_ticks": 2,
                        "max_shipment": 7000,
                        "status": "AVAILABLE",
                    }
                )
            ],
            supply_arrivals=[
                SupplyArrival.model_validate(
                    {
                        "id": "supply-001",
                        "depot_id": "depot-gazipur",
                        "fuel_type": "DIESEL",
                        "quantity": 18000,
                        "planned_tick": 12,
                        "actual_tick": None,
                        "status": "SCHEDULED",
                    }
                )
            ],
            events=[
                SimulatorEvent.model_validate(
                    {
                        "id": 1,
                        "type": "demand_spike",
                        "start_tick": 8,
                        "end_tick": 20,
                        "status": "RESOLVED",
                        "parameters": {"region_ids": ["region-dhaka"], "multiplier": 1.8},
                    }
                )
            ],
            allocations=[
                Allocation.model_validate(
                    {
                        "id": 1,
                        "idempotency_key": "demo-001",
                        "source_depot_id": "depot-gazipur",
                        "destination_station_id": "station-mirpur",
                        "route_id": "route-gazipur-mirpur",
                        "fuel_type": "DIESEL",
                        "quantity": 3000,
                        "created_tick": 5,
                        "departure_tick": 6,
                        "expected_arrival_tick": 8,
                        "actual_arrival_tick": None,
                        "status": "IN_TRANSIT",
                        "failure_reason": None,
                    }
                )
            ],
            metrics=Metrics.model_validate(
                {
                    "served_demand_liters": 1000.0,
                    "unmet_demand_liters": 10.0,
                    "service_level": 0.99,
                    "allocation_liters": 500.0,
                    "allocation_failures": 0,
                }
            ),
            stale=stale,
        )
    )
    if degraded:
        cache.mark_degraded("simulator unreachable")
    return cache


@pytest.fixture
def client() -> TestClient:
    """A TestClient wired to the fakes."""
    app.dependency_overrides[get_simulator] = lambda: _FakeSimulator()
    app.dependency_overrides[get_database] = lambda: _FakeDatabase()
    app.dependency_overrides[get_cache] = lambda: _populated_cache()
    app.dependency_overrides[get_sync] = lambda: _FakeSync()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# -- the read endpoints ----------------------------------------------------


def test_state_returns_full_snapshot(client: TestClient) -> None:
    response = client.get("/api/state")

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

    assert body["instance"]["tick"] == 3
    assert body["stations"][0]["id"] == "station-mirpur"
    assert body["metrics"]["service_level"] == 0.99
    assert body["stale"] is False
    assert body["degraded"] is False


def test_stations_endpoint(client: TestClient) -> None:
    response = client.get("/api/stations")

    assert response.status_code == 200
    body = response.json()
    assert body["stations"][0]["id"] == "station-mirpur"
    assert body["stale"] is False
    assert body["degraded"] is False


def test_depots_endpoint(client: TestClient) -> None:
    response = client.get("/api/depots")

    assert response.status_code == 200
    assert response.json()["depots"][0]["id"] == "depot-gazipur"


def test_routes_endpoint(client: TestClient) -> None:
    response = client.get("/api/routes")

    assert response.status_code == 200
    assert response.json()["routes"][0]["id"] == "route-gazipur-mirpur"


def test_events_endpoint(client: TestClient) -> None:
    response = client.get("/api/events")

    assert response.status_code == 200
    assert response.json()["events"][0]["type"] == "demand_spike"


def test_supply_arrivals_endpoint(client: TestClient) -> None:
    response = client.get("/api/supply-arrivals")

    assert response.status_code == 200
    assert response.json()["supply_arrivals"][0]["id"] == "supply-001"


def test_allocations_endpoint(client: TestClient) -> None:
    response = client.get("/api/allocations")

    assert response.status_code == 200
    assert response.json()["allocations"][0]["idempotency_key"] == "demo-001"


def test_history_endpoint(client: TestClient) -> None:
    response = client.get("/api/history/station-mirpur")

    assert response.status_code == 200
    body = response.json()
    assert body["station_id"] == "station-mirpur"
    assert "observations" in body


def test_history_endpoint_rejects_an_empty_station_id(client: TestClient) -> None:
    response = client.get("/api/history/")

    assert response.status_code in (404, 422)


# -- degraded mode ---------------------------------------------------------


def test_endpoints_report_degraded_mode() -> None:
    app.dependency_overrides[get_simulator] = lambda: _FakeSimulator(unreachable=True)
    app.dependency_overrides[get_database] = lambda: _FakeDatabase()
    app.dependency_overrides[get_cache] = lambda: _populated_cache(degraded=True)
    app.dependency_overrides[get_sync] = lambda: _FakeSync()
    with TestClient(app) as test_client:
        response = test_client.get("/api/stations")
        health = test_client.get("/health")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert body["degraded_reason"] == "simulator unreachable"
    assert body["age_seconds"] is not None
    # The data is still served.
    assert body["stations"][0]["id"] == "station-mirpur"

    assert health.status_code == 503
    assert health.json()["components"]["simulator"]["status"] == "unreachable"


def test_endpoints_report_stale_data() -> None:
    app.dependency_overrides[get_simulator] = lambda: _FakeSimulator(stale=True)
    app.dependency_overrides[get_database] = lambda: _FakeDatabase()
    app.dependency_overrides[get_cache] = lambda: _populated_cache(stale=True)
    app.dependency_overrides[get_sync] = lambda: _FakeSync()
    with TestClient(app) as test_client:
        response = test_client.get("/api/depots")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["stale"] is True


def test_state_is_503_when_no_state_was_ever_fetched() -> None:
    app.dependency_overrides[get_simulator] = lambda: _FakeSimulator()
    app.dependency_overrides[get_database] = lambda: _FakeDatabase()
    app.dependency_overrides[get_cache] = lambda: StateCache()
    app.dependency_overrides[get_sync] = lambda: _FakeSync()
    with TestClient(app) as test_client:
        response = test_client.get("/api/state")
    app.dependency_overrides.clear()

    assert response.status_code == 503


# -- health ----------------------------------------------------------------


def test_health_reports_all_components(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["components"]["backend"]["status"] == "ok"
    assert body["components"]["database"]["status"] == "ok"
    assert body["components"]["simulator"]["status"] == "ok"
    assert body["components"]["simulator"]["simulation"]["tick"] == 3
    assert "sync" in body


def test_health_is_503_when_the_simulator_is_down() -> None:
    app.dependency_overrides[get_simulator] = lambda: _FakeSimulator(unreachable=True)
    app.dependency_overrides[get_database] = lambda: _FakeDatabase()
    app.dependency_overrides[get_cache] = lambda: _populated_cache(degraded=True)
    app.dependency_overrides[get_sync] = lambda: _FakeSync()
    with TestClient(app) as test_client:
        response = test_client.get("/health")
    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_root_banner(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["health"] == "/health"


def test_metrics_endpoint_is_exposed(client: TestClient) -> None:
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "http_requests_total" in response.text
