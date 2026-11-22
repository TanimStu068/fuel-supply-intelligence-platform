"""Building a world snapshot from the simulator's live state.

The decision engine reasons over a frozen copy of the network rather than
the live Pydantic models. This module is the boundary between the two: it
takes the simulator's own types and produces the plain snapshot the
optimizer reads.
"""

from __future__ import annotations

from datetime import datetime

from ..simulator_models import (
    Allocation,
    Depot,
    Route,
    Station,
    SupplyArrival,
)
from .world import (
    AllocationSnapshot,
    DepotSnapshot,
    RouteSnapshot,
    StationSnapshot,
    SupplyArrivalSnapshot,
    WorldSnapshot,
)


def build_world(
    *,
    tick: int,
    sim_time: datetime | None,
    tick_minutes: int,
    depots: list[Depot],
    stations: list[Station],
    routes: list[Route],
    allocations: list[Allocation],
    supply_arrivals: list[SupplyArrival],
) -> WorldSnapshot:
    """Build a frozen snapshot of the network."""
    return WorldSnapshot(
        tick=tick,
        sim_time=sim_time,
        tick_minutes=tick_minutes,
        depots={
            depot.id: DepotSnapshot(
                id=depot.id,
                status=depot.status.value,
                dispatch_capacity_per_tick=depot.dispatch_capacity_per_tick,
                inventory={fuel.value: amount for fuel, amount in depot.inventory.items()},
                capacity={fuel.value: amount for fuel, amount in depot.capacity.items()},
            )
            for depot in depots
        },
        stations={
            station.id: StationSnapshot(
                id=station.id,
                status=station.status.value,
                inventory={fuel.value: amount for fuel, amount in station.inventory.items()},
                capacity={fuel.value: amount for fuel, amount in station.capacity.items()},
            )
            for station in stations
        },
        routes=tuple(
            RouteSnapshot(
                id=route.id,
                source_depot_id=route.source_depot_id,
                destination_station_id=route.destination_station_id,
                transit_ticks=route.transit_ticks,
                max_shipment=route.max_shipment,
                status=route.status.value,
            )
            for route in routes
        ),
        allocations=tuple(
            AllocationSnapshot(
                id=allocation.id,
                idempotency_key=allocation.idempotency_key,
                source_depot_id=allocation.source_depot_id,
                destination_station_id=allocation.destination_station_id,
                route_id=allocation.route_id,
                fuel_type=allocation.fuel_type.value,
                quantity=allocation.quantity,
                created_tick=allocation.created_tick,
                expected_arrival_tick=allocation.expected_arrival_tick,
                status=allocation.status.value,
            )
            for allocation in allocations
        ),
        supply_arrivals=tuple(
            SupplyArrivalSnapshot(
                id=arrival.id,
                depot_id=arrival.depot_id,
                fuel_type=arrival.fuel_type.value,
                quantity=arrival.quantity,
                planned_tick=arrival.planned_tick,
                actual_tick=arrival.actual_tick,
                status=arrival.status.value,
            )
            for arrival in supply_arrivals
        ),
    )
