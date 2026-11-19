"""Happy-path tests for the simulator client."""

from __future__ import annotations

import json

import httpx
import pytest

from app.simulator_client import SimulatorClient
from app.simulator_models import (
    AllocationStatus,
    DepotStatus,
    FuelType,
    InstanceStatus,
    RouteStatus,
    StationStatus,
)

INSTANCE_PAYLOAD = {
    "id": 1,
    "scenario_id": "baseline",
    "scenario_version": "1.0",
    "seed": 12345,
    "sim_time": "2026-01-01T00:00:00+00:00",
    "tick": 7,
    "tick_minutes": 15,
    "status": "RUNNING",
}

HEALTH_PAYLOAD = {
    "status": "ok",
    "database": "ok",
    "simulation": {"status": "RUNNING", "tick": 7},
}

DEPOT_PAYLOAD = {
    "id": "depot-gazipur",
    "name": "Gazipur Depot",
    "region_id": "region-dhaka",
    "status": "OPEN",
    "dispatch_capacity_per_tick": 12000,
    "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
    "inventory": {"DIESEL": 60000, "PETROL": 45000, "OCTANE": 26000},
}

STATION_PAYLOAD = {
    "id": "station-mirpur",
    "name": "Mirpur Fuel Station",
    "region_id": "region-dhaka",
    "status": "OPEN",
    "demand_profile": "urban_high",
    "demand_multiplier": 1.0,
    "capacity": {"DIESEL": 15000, "PETROL": 14000, "OCTANE": 9000},
    "inventory": {"DIESEL": 9000, "PETROL": 9000, "OCTANE": 5000},
}

ROUTE_PAYLOAD = {
    "id": "route-gazipur-mirpur",
    "source_depot_id": "depot-gazipur",
    "destination_station_id": "station-mirpur",
    "transit_ticks": 2,
    "max_shipment": 7000,
    "status": "AVAILABLE",
}

ALLOCATION_PAYLOAD = {
    "id": 1,
    "idempotency_key": "demo-001",
    "source_depot_id": "depot-gazipur",
    "destination_station_id": "station-mirpur",
    "route_id": "route-gazipur-mirpur",
    "fuel_type": "DIESEL",
    "quantity": 3000,
    "created_tick": 5,
    "departure_tick": None,
    "expected_arrival_tick": None,
    "actual_arrival_tick": None,
    "status": "PENDING",
    "failure_reason": None,
}


async def test_get_instance_returns_validated_model(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/instance"
        return httpx.Response(200, json=INSTANCE_PAYLOAD)

    client = client_factory(handler)
    instance = await client.get_instance()

    assert instance.tick == 7
    assert instance.status is InstanceStatus.RUNNING
    assert instance.seed == 12345
    assert client.stale is False


async def test_get_health(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=HEALTH_PAYLOAD)

    client = client_factory(handler)
    health = await client.health()

    assert health.status == "ok"
    assert health.simulation.tick == 7


async def test_get_depots_parses_fuel_map(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[DEPOT_PAYLOAD])

    client = client_factory(handler)
    depots = await client.get_depots()

    assert len(depots) == 1
    assert depots[0].status is DepotStatus.OPEN
    assert depots[0].inventory[FuelType.DIESEL] == 60000


async def test_get_stations_and_routes(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/stations":
            return httpx.Response(200, json=[STATION_PAYLOAD])
        return httpx.Response(200, json=[ROUTE_PAYLOAD])

    client = client_factory(handler)

    stations = await client.get_stations()
    routes = await client.get_routes()

    assert stations[0].status is StationStatus.OPEN
    assert stations[0].demand_multiplier == 1.0
    assert routes[0].status is RouteStatus.AVAILABLE
    assert routes[0].max_shipment == 7000


async def test_create_allocation_accepts_201(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/allocations"
        payload = json.loads(request.content)
        assert payload["idempotency_key"] == "demo-001"
        assert payload["fuel_type"] == "DIESEL"
        assert payload["quantity"] == 3000
        return httpx.Response(201, json=ALLOCATION_PAYLOAD)

    client = client_factory(handler)
    allocation = await client.create_allocation(
        {
            "idempotency_key": "demo-001",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 3000,
        }
    )

    assert allocation.id == 1
    assert allocation.status is AllocationStatus.PENDING
    assert allocation.fuel_type is FuelType.DIESEL


async def test_create_allocation_accepts_200_on_idempotent_replay(client_factory) -> None:
    """The simulator may answer 200 for a replay; treat it as success."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ALLOCATION_PAYLOAD)

    client = client_factory(handler)
    allocation = await client.create_allocation(
        {
            "idempotency_key": "demo-001",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 3000,
        }
    )

    assert allocation.id == 1


async def test_cancel_allocation(client_factory) -> None:
    cancelled = {**ALLOCATION_PAYLOAD, "status": "CANCELLED"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/allocations/1/cancel"
        return httpx.Response(200, json=cancelled)

    client = client_factory(handler)
    allocation = await client.cancel_allocation(1)

    assert allocation.status is AllocationStatus.CANCELLED


async def test_admin_step_response(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/step"
        return httpx.Response(200, json={"tick": 6, "sim_time": "2026-01-01T01:30:00+00:00"})

    client = client_factory(handler)
    result = await client.admin_step()

    assert result.tick == 6


async def test_admin_run_returns_instance_shape(client_factory) -> None:
    """``/admin/run`` returns the serialized simulation instance."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/run"
        return httpx.Response(200, json=INSTANCE_PAYLOAD)

    client = client_factory(handler)
    result = await client.admin_run()

    assert result.status is None or isinstance(result.status, str)


async def test_get_demand_history_passes_query_params(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/demand-history"
        assert request.url.params["station_id"] == "station-mirpur"
        assert request.url.params["limit"] == "50"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 100,
                    "station_id": "station-mirpur",
                    "fuel_type": "DIESEL",
                    "tick": 12,
                    "sim_time": "2026-01-01T03:00:00+00:00",
                    "demand_liters": 95.123,
                    "served_liters": 95.123,
                    "unmet_liters": 0.0,
                }
            ],
        )

    client = client_factory(handler)
    rows = await client.get_demand_history(station_id="station-mirpur", limit=50)

    assert len(rows) == 1
    assert rows[0].demand_liters == 95.123


async def test_get_metrics(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "served_demand_liters": 12345.678,
                "unmet_demand_liters": 234.567,
                "service_level": 0.981408,
                "allocation_liters": 9800.0,
                "allocation_failures": 2,
            },
        )

    client = client_factory(handler)
    metrics = await client.get_metrics()

    assert metrics.service_level == pytest.approx(0.981408)
    assert metrics.allocation_failures == 2


async def test_client_is_reusable_across_calls(client_factory) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/instance":
            return httpx.Response(200, json=INSTANCE_PAYLOAD)
        return httpx.Response(200, json=[])

    client = client_factory(handler)
    await client.get_instance()
    await client.get_routes()
    await client.get_allocations()

    assert calls == ["/v1/instance", "/v1/routes", "/v1/allocations"]


async def test_context_manager_closes_owned_client(settings) -> None:
    async with SimulatorClient(settings=settings) as client:
        assert client is not None
