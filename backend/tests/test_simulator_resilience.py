"""Resilience tests: 503 retry with backoff, stale header, malformed payloads."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.simulator_errors import (
    CannotCancelError,
    DispatchCapacityExceededError,
    FaultInjectedError,
    IdempotencyKeyMismatchError,
    InsufficientInventoryError,
    RouteDisruptedError,
    RouteMismatchError,
    SimulatorAPIError,
    SimulatorTimeoutError,
    SimulatorValidationError,
)
from app.simulator_models import FuelType

INSTANCE_PAYLOAD = {
    "id": 1,
    "scenario_id": "baseline",
    "scenario_version": "1.0",
    "seed": 12345,
    "sim_time": "2026-01-01T00:00:00+00:00",
    "tick": 0,
    "tick_minutes": 15,
    "status": "PAUSED",
}

FAULT_BODY = {
    "error": {
        "code": "FAULT_INJECTED",
        "message": "Simulator API temporarily unavailable.",
    }
}


async def test_retries_on_503_fault_injected_then_succeeds(client_factory) -> None:
    """A 503 with FAULT_INJECTED is retried with backoff, then succeeds."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return httpx.Response(503, json=FAULT_BODY)
        return httpx.Response(200, json=INSTANCE_PAYLOAD)

    client = client_factory(handler)
    instance = await client.get_instance()

    assert instance.tick == 0
    assert attempts == 3  # two failures + one success


async def test_gives_up_after_max_retries(client_factory) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json=FAULT_BODY)

    client = client_factory(handler)

    with pytest.raises(FaultInjectedError) as excinfo:
        await client.get_instance()

    assert excinfo.value.status_code == 503
    assert excinfo.value.code == "FAULT_INJECTED"
    assert excinfo.value.is_fault_injected is True
    assert attempts == 3  # initial attempt + 2 retries


async def test_retries_on_transport_error(client_factory) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=INSTANCE_PAYLOAD)

    client = client_factory(handler)
    instance = await client.get_instance()

    assert instance.tick == 0
    assert attempts == 2


async def test_timeout_is_typed(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = client_factory(handler)

    with pytest.raises(SimulatorTimeoutError):
        await client.get_instance()


async def test_stale_header_sets_flag(client_factory) -> None:
    """``X-Simulator-Stale: true`` must be surfaced as a flag."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=INSTANCE_PAYLOAD, headers={"X-Simulator-Stale": "true"})

    client = client_factory(handler)
    await client.get_instance()

    assert client.stale is True


async def test_stale_header_absent_leaves_flag_false(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=INSTANCE_PAYLOAD)

    client = client_factory(handler)
    await client.get_instance()

    assert client.stale is False


async def test_stale_header_resets_between_calls(client_factory) -> None:
    """The sticky flag is cleared explicitly via ``reset_stale``."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=INSTANCE_PAYLOAD, headers={"X-Simulator-Stale": "true"})
        return httpx.Response(200, json=INSTANCE_PAYLOAD)

    client = client_factory(handler)
    await client.get_instance()
    assert client.stale is True

    client.reset_stale()
    await client.get_instance()
    assert client.stale is False


async def test_malformed_payload_is_rejected(client_factory) -> None:
    """A payload violating the documented schema must be rejected."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "not-an-int", "status": "RUNNING"})

    client = client_factory(handler)

    with pytest.raises(SimulatorValidationError) as excinfo:
        await client.get_instance()

    assert "/v1/instance" in str(excinfo.value)


async def test_unknown_fuel_type_is_rejected(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "depot-gazipur",
                    "name": "Gazipur Depot",
                    "region_id": "region-dhaka",
                    "status": "OPEN",
                    "dispatch_capacity_per_tick": 12000,
                    "capacity": {"DIESEL": 90000, "Kerosene": 1},
                    "inventory": {"DIESEL": 60000, "Kerosene": 1},
                }
            ],
        )

    client = client_factory(handler)

    with pytest.raises(SimulatorValidationError):
        await client.get_depots()


async def test_unknown_status_value_is_rejected(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "depot-gazipur",
                "name": "Gazipur Depot",
                "region_id": "region-dhaka",
                "status": "ON_FIRE",
                "dispatch_capacity_per_tick": 12000,
                "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
                "inventory": {"DIESEL": 60000, "PETROL": 45000, "OCTANE": 26000},
            },
        )

    client = client_factory(handler)

    with pytest.raises(SimulatorValidationError):
        await client.get_depots()


async def test_non_json_body_is_rejected(client_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    client = client_factory(handler)

    with pytest.raises(SimulatorValidationError):
        await client.get_instance()


async def test_detail_envelope_maps_to_typed_exception(client_factory) -> None:
    """Domain errors use ``{"detail": {...}}`` and map to typed exceptions."""
    body = {"detail": {"code": "ROUTE_MISMATCH", "message": "Route does not connect"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=body)

    client = client_factory(handler)

    with pytest.raises(RouteMismatchError) as excinfo:
        await client.create_allocation(
            {
                "idempotency_key": "demo-002",
                "source_depot_id": "depot-gazipur",
                "destination_station_id": "station-mirpur",
                "route_id": "route-gazipur-tongi",
                "fuel_type": "DIESEL",
                "quantity": 1000,
            }
        )

    assert excinfo.value.code == "ROUTE_MISMATCH"
    assert excinfo.value.status_code == 409


async def test_error_envelope_maps_to_typed_exception(client_factory) -> None:
    """Injected faults use ``{"error": {...}}`` and map to the same hierarchy."""
    body = {"error": {"code": "FAULT_INJECTED", "message": "down"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json=body)

    client = client_factory(handler)

    with pytest.raises(FaultInjectedError):
        await client.get_instance()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("IDEMPOTENCY_KEY_MISMATCH", IdempotencyKeyMismatchError),
        ("ROUTE_MISMATCH", RouteMismatchError),
        ("INSUFFICIENT_INVENTORY", InsufficientInventoryError),
        ("DISPATCH_CAPACITY_EXCEEDED", DispatchCapacityExceededError),
        ("ROUTE_DISRUPTED", RouteDisruptedError),
        ("CANNOT_CANCEL", CannotCancelError),
    ],
)
async def test_all_409_codes_map_to_typed_exceptions(client_factory, code, expected) -> None:
    body = {"detail": {"code": code, "message": "conflict"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=body)

    client = client_factory(handler)

    with pytest.raises(expected):
        await client.cancel_allocation(1)


async def test_unknown_409_code_falls_back_to_conflict_error(client_factory) -> None:
    body = {"detail": {"code": "SOMETHING_NEW", "message": "conflict"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=body)

    client = client_factory(handler)

    with pytest.raises(SimulatorAPIError) as excinfo:
        await client.cancel_allocation(1)

    assert excinfo.value.code == "SOMETHING_NEW"


async def test_404_maps_to_not_found(client_factory) -> None:
    body = {"detail": {"code": "NOT_FOUND"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=body)

    client = client_factory(handler)

    with pytest.raises(SimulatorAPIError) as excinfo:
        await client.get_depot("depot-does-not-exist")

    assert excinfo.value.status_code == 404


async def test_422_fastapi_validation_body_is_still_typed(client_factory) -> None:
    """FastAPI's ``{"detail": [...]}`` list body must not crash the parser."""
    body = {"detail": [{"loc": ["body", "quantity"], "msg": "Input should be greater than 0"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json=body)

    client = client_factory(handler)

    with pytest.raises(SimulatorAPIError) as excinfo:
        await client.get_instance()

    assert excinfo.value.status_code == 422


async def test_client_side_validation_rejects_bad_request(client_factory) -> None:
    """Bad request payloads fail locally, before hitting the network."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("network should not be reached")

    client = client_factory(handler)

    with pytest.raises(ValidationError):
        await client.create_allocation(
            {
                "idempotency_key": "bad",
                "source_depot_id": "depot-gazipur",
                "destination_station_id": "station-mirpur",
                "route_id": "route-gazipur-mirpur",
                "fuel_type": "KEROSENE",
                "quantity": 10,
            }
        )


async def test_fault_injection_error_body_is_not_retried_when_not_503(client_factory) -> None:
    """A 409 that happens to use the ``error`` envelope is not retried."""
    body = {"error": {"code": "ROUTE_DISRUPTED", "message": "route down"}}
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(409, json=body)

    client = client_factory(handler)

    with pytest.raises(RouteDisruptedError):
        await client.create_allocation(
            {
                "idempotency_key": "demo-003",
                "source_depot_id": "depot-gazipur",
                "destination_station_id": "station-mirpur",
                "route_id": "route-gazipur-mirpur",
                "fuel_type": FuelType.DIESEL.value,
                "quantity": 500,
            }
        )

    assert attempts == 1
