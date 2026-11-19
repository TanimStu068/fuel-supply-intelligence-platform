"""Tests for the FastAPI app: /health and /api/state."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_database, get_simulator


class _FakeSimulator:
    """Minimal stand-in for :class:`SimulatorClient`."""

    def __init__(self, stale: bool = False) -> None:
        self.stale = stale
        self.last_stale = stale

    def reset_stale(self) -> None:
        """No-op: the fake always reports its configured staleness."""
        return None

    async def health(self):
        from app.simulator_models import HealthResponse

        return HealthResponse.model_validate(
            {
                "status": "ok",
                "database": "ok",
                "simulation": {"status": "RUNNING", "tick": 3},
            }
        )

    async def get_instance(self):
        from app.simulator_models import Instance

        return Instance.model_validate(
            {
                "id": 1,
                "scenario_id": "baseline",
                "scenario_version": "1.0",
                "seed": 12345,
                "sim_time": "2026-01-01T00:45:00+00:00",
                "tick": 3,
                "tick_minutes": 15,
                "status": "RUNNING",
            }
        )

    async def get_regions(self):
        from app.simulator_models import Region

        return [
            Region.model_validate(
                {"id": "region-dhaka", "name": "Dhaka Division", "demand_factor": 1.0}
            )
        ]

    async def get_depots(self):
        from app.simulator_models import Depot

        return [
            Depot.model_validate(
                {
                    "id": "depot-gazipur",
                    "name": "Gazipur Depot",
                    "region_id": "region-dhaka",
                    "status": "OPEN",
                    "dispatch_capacity_per_tick": 12000,
                    "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
                    "inventory": {"DIESEL": 60000, "PETROL": 45000, "OCTANE": 26000},
                }
            )
        ]

    async def get_stations(self):
        from app.simulator_models import Station

        return [
            Station.model_validate(
                {
                    "id": "station-mirpur",
                    "name": "Mirpur Fuel Station",
                    "region_id": "region-dhaka",
                    "status": "OPEN",
                    "demand_profile": "urban_high",
                    "demand_multiplier": 1.0,
                    "capacity": {"DIESEL": 15000, "PETROL": 14000, "OCTANE": 9000},
                    "inventory": {"DIESEL": 9000, "PETROL": 9000, "OCTANE": 5000},
                }
            )
        ]

    async def get_routes(self):
        from app.simulator_models import Route

        return [
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
        ]

    async def get_supply_arrivals(self):
        from app.simulator_models import SupplyArrival

        return [
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
        ]

    async def get_events(self):
        return []

    async def get_allocations(self):
        return []

    async def get_metrics(self):
        from app.simulator_models import Metrics

        return Metrics.model_validate(
            {
                "served_demand_liters": 1000.0,
                "unmet_demand_liters": 10.0,
                "service_level": 0.99,
                "allocation_liters": 500.0,
                "allocation_failures": 0,
            }
        )


class _FakeDatabase:
    async def ping(self) -> bool:
        return True


@pytest.fixture
def client() -> TestClient:
    """A TestClient wired to the fake simulator and database."""
    app.dependency_overrides[get_simulator] = lambda: _FakeSimulator()
    app.dependency_overrides[get_database] = lambda: _FakeDatabase()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_health_reports_all_components(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["components"]["backend"]["status"] == "ok"
    assert body["components"]["database"]["status"] == "ok"
    assert body["components"]["simulator"]["status"] == "ok"
    assert body["components"]["simulator"]["simulation"]["tick"] == 3


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
    ):
        assert key in body, f"missing section: {key}"

    assert body["instance"]["tick"] == 3
    assert body["depots"][0]["id"] == "depot-gazipur"
    assert body["metrics"]["service_level"] == 0.99
    assert body["stale"] is False


def test_state_surfaces_stale_flag() -> None:
    app.dependency_overrides[get_simulator] = lambda: _FakeSimulator(stale=True)
    app.dependency_overrides[get_database] = lambda: _FakeDatabase()
    with TestClient(app) as test_client:
        response = test_client.get("/api/state")
    app.dependency_overrides.clear()

    assert response.json()["stale"] is True


def test_root_banner(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["health"] == "/health"


def test_health_is_503_when_database_is_down() -> None:
    class _DownDatabase:
        async def ping(self) -> bool:
            return False

    app.dependency_overrides[get_simulator] = lambda: _FakeSimulator()
    app.dependency_overrides[get_database] = lambda: _DownDatabase()
    with TestClient(app) as test_client:
        response = test_client.get("/health")
    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_metrics_endpoint_is_exposed(client: TestClient) -> None:
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "http_requests_total" in response.text
