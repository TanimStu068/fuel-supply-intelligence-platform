"""Tests for the dispatcher: the part that actually moves fuel."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.intelligence.dispatcher import Dispatcher
from app.intelligence.forecasting import Forecast, ForecastPoint, ModelKind
from app.intelligence.risk import RiskAssessment, RiskLevel

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


class FakeStation:
    """A stand-in for the simulator's station."""

    def __init__(
        self,
        station_id: str,
        inventory: dict[str, float],
        capacity: dict[str, float],
        status: str = "OPEN",
    ) -> None:
        self.id = station_id
        self.inventory = inventory
        self.capacity = capacity
        self.status = status


class FakeDepot:
    """A stand-in for the simulator's depot."""

    def __init__(
        self,
        depot_id: str,
        inventory: dict[str, float],
        dispatch_capacity_per_tick: float = 12000.0,
        status: str = "OPEN",
    ) -> None:
        self.id = depot_id
        self.inventory = inventory
        self.dispatch_capacity_per_tick = dispatch_capacity_per_tick
        self.status = status


class FakeRoute:
    """A stand-in for the simulator's route."""

    def __init__(
        self,
        route_id: str,
        source_depot_id: str,
        destination_station_id: str,
        transit_ticks: int = 2,
        max_shipment: float = 7000.0,
        status: str = "AVAILABLE",
    ) -> None:
        self.id = route_id
        self.source_depot_id = source_depot_id
        self.destination_station_id = destination_station_id
        self.transit_ticks = transit_ticks
        self.max_shipment = max_shipment
        self.status = type("Status", (), {"value": status})()


class FakeAllocation:
    """A stand-in for the simulator's allocation."""

    def __init__(
        self,
        source_depot_id: str,
        destination_station_id: str,
        fuel_type: str,
        quantity: float,
        status: str = "PENDING",
        created_tick: int = 0,
    ) -> None:
        self.source_depot_id = source_depot_id
        self.destination_station_id = destination_station_id
        self.fuel_type = type("Fuel", (), {"value": fuel_type})()
        self.quantity = quantity
        self.status = type("Status", (), {"value": status})()
        self.created_tick = created_tick


def _forecast(per_tick: float = 100.0, ticks: int = 24) -> Forecast:
    """A forecast with a constant demand per tick."""
    return Forecast(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        model=ModelKind.LEARNED,
        horizon_ticks=ticks,
        points=[
            ForecastPoint(
                tick=11 + offset,
                sim_time=BASE_TIME,
                predicted_demand=per_tick,
                lower_bound=per_tick * 0.9,
                upper_bound=per_tick * 1.1,
                confidence=0.9,
            )
            for offset in range(ticks)
        ],
        confidence=0.9,
        needs_human_review=False,
    )


def _risk(
    *,
    station_id: str = "station-mirpur",
    fuel_type: str = "DIESEL",
    level: RiskLevel = RiskLevel.CRITICAL,
    hours: float | None = 4.0,
) -> RiskAssessment:
    """A risk assessment for the tests."""
    return RiskAssessment(
        station_id=station_id,
        fuel_type=fuel_type,
        risk_level=level,
        hours_to_stockout=hours,
        stockout_probability=1.0,
        risk_score=0.9,
        forecast=_forecast(),
        needs_human_review=False,
        recommendation="Dispatch now.",
    )


class FakeClient:
    """A simulator client that records the allocations it was asked for."""

    def __init__(self, error: Exception | None = None) -> None:
        self.submitted: list[dict] = []
        self.error = error

    async def create_allocation(self, allocation: dict) -> object:
        if self.error is not None:
            raise self.error
        self.submitted.append(allocation)
        return type(
            "Allocation",
            (),
            {"id": len(self.submitted), "status": type("Status", (), {"value": "PENDING"})()},
        )()


def _world(**overrides):
    """A small world: one station short of fuel, one depot with plenty."""
    station = FakeStation(
        "station-mirpur",
        {"DIESEL": 100.0, "PETROL": 100.0},
        {"DIESEL": 15000.0, "PETROL": 14000.0},
    )
    depot = FakeDepot("depot-gazipur", {"DIESEL": 90000.0, "PETROL": 70000.0})
    route = FakeRoute("route-gazipur-mirpur", "depot-gazipur", "station-mirpur", 2, 7000.0)
    return {
        "stations": {station.id: station},
        "depots": {depot.id: depot},
        "routes": [route],
        "allocations": [],
        **overrides,
    }


@pytest.mark.asyncio
async def test_a_short_station_gets_fuel() -> None:
    client = FakeClient()
    dispatcher = Dispatcher(client)
    world = _world()

    decisions = await dispatcher.dispatch(
        risks=[_risk()],
        tick=10,
        **world,
    )

    assert len(decisions) == 1
    assert decisions[0].submitted is True
    assert decisions[0].station_id == "station-mirpur"
    assert decisions[0].fuel_type == "DIESEL"
    assert decisions[0].quantity > 0
    assert client.submitted[0]["destination_station_id"] == "station-mirpur"


@pytest.mark.asyncio
async def test_a_well_stocked_station_is_left_alone() -> None:
    client = FakeClient()
    dispatcher = Dispatcher(client)
    station = FakeStation("station-mirpur", {"DIESEL": 14000.0}, {"DIESEL": 15000.0})
    world = _world(stations={station.id: station})

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions == []
    assert client.submitted == []


@pytest.mark.asyncio
async def test_a_shipment_that_rounds_to_zero_is_not_submitted() -> None:
    """A quantity that rounds to zero litres must never be sent.

    The simulator rejects a zero quantity outright, so a shipment of a
    fraction of a litre is a guaranteed failure rather than a small
    shipment.
    """
    client = FakeClient()
    dispatcher = Dispatcher(client)
    # The station has 500.4 L of room, so the shipment rounds to zero.
    station = FakeStation("station-mirpur", {"DIESEL": 500.0}, {"DIESEL": 500.4})
    world = _world(stations={station.id: station})

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions == []
    assert client.submitted == []


@pytest.mark.asyncio
async def test_a_station_in_outage_is_skipped() -> None:
    client = FakeClient()
    dispatcher = Dispatcher(client)
    station = FakeStation(
        "station-mirpur",
        {"DIESEL": 0.0},
        {"DIESEL": 15000.0},
        status="OUTAGE",
    )
    world = _world(stations={station.id: station})

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions == []


@pytest.mark.asyncio
async def test_the_quantity_respects_the_route_limit() -> None:
    client = FakeClient()
    dispatcher = Dispatcher(client)
    route = FakeRoute("route-gazipur-mirpur", "depot-gazipur", "station-mirpur", 2, 500.0)
    world = _world(routes=[route])

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions[0].quantity <= 500.0


@pytest.mark.asyncio
async def test_the_quantity_respects_the_dispatch_capacity() -> None:
    """The simulator refuses a depot more than its per-tick allowance."""
    client = FakeClient()
    dispatcher = Dispatcher(client)
    depot = FakeDepot("depot-gazipur", {"DIESEL": 90000.0}, dispatch_capacity_per_tick=1000.0)
    world = _world(depots={depot.id: depot})

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions[0].quantity <= 1000.0


@pytest.mark.asyncio
async def test_the_capacity_already_used_this_tick_is_respected() -> None:
    """Allocations created on this tick count against the depot's limit."""
    client = FakeClient()
    dispatcher = Dispatcher(client)
    depot = FakeDepot("depot-gazipur", {"DIESEL": 90000.0}, dispatch_capacity_per_tick=1000.0)
    world = _world(
        depots={depot.id: depot},
        allocations=[
            FakeAllocation("depot-gazipur", "station-tongi", "DIESEL", 900.0, created_tick=10)
        ],
    )

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions[0].quantity <= 100.0


@pytest.mark.asyncio
async def test_an_allocation_from_an_earlier_tick_does_not_use_the_allowance() -> None:
    """The simulator's allowance resets when the tick advances."""
    client = FakeClient()
    dispatcher = Dispatcher(client)
    depot = FakeDepot("depot-gazipur", {"DIESEL": 90000.0}, dispatch_capacity_per_tick=1000.0)
    world = _world(
        depots={depot.id: depot},
        allocations=[
            FakeAllocation("depot-gazipur", "station-tongi", "DIESEL", 900.0, created_tick=9)
        ],
    )

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions[0].quantity <= 1000.0


@pytest.mark.asyncio
async def test_fuel_already_in_transit_is_not_sent_again() -> None:
    client = FakeClient()
    dispatcher = Dispatcher(client)
    world = _world(
        allocations=[
            FakeAllocation("depot-gazipur", "station-mirpur", "DIESEL", 50000.0, created_tick=9)
        ],
    )

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions == []


@pytest.mark.asyncio
async def test_a_disrupted_route_is_not_used() -> None:
    client = FakeClient()
    dispatcher = Dispatcher(client)
    route = FakeRoute(
        "route-gazipur-mirpur",
        "depot-gazipur",
        "station-mirpur",
        2,
        7000.0,
        status="DISRUPTED",
    )
    world = _world(routes=[route])

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions == []


@pytest.mark.asyncio
async def test_an_ok_station_is_never_dispatched_to() -> None:
    client = FakeClient()
    dispatcher = Dispatcher(client)
    world = _world()

    decisions = await dispatcher.dispatch(
        risks=[_risk(level=RiskLevel.OK, hours=None)], tick=10, **world
    )

    assert decisions == []


@pytest.mark.asyncio
async def test_a_rejected_shipment_is_recorded_not_raised() -> None:
    """The simulator's 409 must not take the platform down."""
    from app.simulator_errors import ConflictError

    client = FakeClient(error=ConflictError(409, "DISPATCH_CAPACITY_EXCEEDED", "too much"))
    dispatcher = Dispatcher(client)
    world = _world()

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert len(decisions) == 1
    assert decisions[0].submitted is False
    assert "rejected" in decisions[0].outcome


@pytest.mark.asyncio
async def test_a_disabled_dispatcher_does_nothing() -> None:
    client = FakeClient()
    dispatcher = Dispatcher(client, enabled=False)
    world = _world()

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions == []
    assert client.submitted == []


# --- the fixes the live run exposed ---------------------------------------


@pytest.mark.asyncio
async def test_a_constrained_depot_is_still_shippable() -> None:
    """The simulator accepts allocations from a CONSTRAINED depot.

    Refusing to use one stops the platform from shipping exactly when the
    network is under pressure.
    """
    client = FakeClient()
    dispatcher = Dispatcher(client)
    depot = FakeDepot("depot-gazipur", {"DIESEL": 90000.0}, status="CONSTRAINED")
    world = _world(depots={depot.id: depot})

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert len(decisions) == 1
    assert decisions[0].submitted is True


@pytest.mark.asyncio
async def test_another_route_is_used_when_the_first_depot_is_empty() -> None:
    """A station is not short because one route's depot happens to be empty."""
    client = FakeClient()
    dispatcher = Dispatcher(client)

    empty_depot = FakeDepot("depot-gazipur", {"DIESEL": 0.0})
    full_depot = FakeDepot("depot-patiya", {"DIESEL": 90000.0})
    routes = [
        FakeRoute("route-gazipur-mirpur", "depot-gazipur", "station-mirpur", 2, 7000.0),
        FakeRoute("route-patiya-mirpur", "depot-patiya", "station-mirpur", 4, 5000.0),
    ]
    world = _world(
        depots={empty_depot.id: empty_depot, full_depot.id: full_depot},
        routes=routes,
    )

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert len(decisions) == 1
    assert decisions[0].source_depot_id == "depot-patiya"
    assert decisions[0].route_id == "route-patiya-mirpur"


@pytest.mark.asyncio
async def test_only_one_shipment_per_station_fuel_and_tick() -> None:
    """The simulator's idempotency key is permanent.

    A second shipment in the same tick with a different quantity would come
    back as a 409 key mismatch.
    """
    client = FakeClient()
    dispatcher = Dispatcher(client)
    world = _world(
        allocations=[
            FakeAllocation("depot-gazipur", "station-mirpur", "DIESEL", 100.0, created_tick=10)
        ],
    )

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions == []
    assert client.submitted == []


@pytest.mark.asyncio
async def test_a_station_is_not_topped_up_past_its_capacity() -> None:
    """The target cover never asks for more than the tank can hold.

    A tank with 600 L of room is sent 600 L, not the whole 24-hour cover.
    """
    client = FakeClient()
    dispatcher = Dispatcher(client)
    station = FakeStation("station-mirpur", {"DIESEL": 400.0}, {"DIESEL": 1000.0})
    world = _world(stations={station.id: station})

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert len(decisions) == 1
    assert decisions[0].quantity <= 600.0


@pytest.mark.asyncio
async def test_a_station_with_only_a_little_room_is_left_alone() -> None:
    """Filling the last 100 L of a tank is not worth a shipment."""
    client = FakeClient()
    dispatcher = Dispatcher(client)
    station = FakeStation("station-mirpur", {"DIESEL": 900.0}, {"DIESEL": 1000.0})
    world = _world(stations={station.id: station})

    decisions = await dispatcher.dispatch(risks=[_risk()], tick=10, **world)

    assert decisions == []
    assert client.submitted == []
