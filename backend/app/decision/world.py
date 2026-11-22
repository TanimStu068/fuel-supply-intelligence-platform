"""The world the decision engine reasons about.

The engine works on plain, immutable snapshots of the simulator's state
rather than on the live Pydantic models. Two reasons:

* the snapshot can be taken once and used for *every* station and fuel, so
  the optimizer sees one consistent world instead of a moving target; and
* it makes the what-if simulation honest — the projection runs against a
  frozen copy of the world, never against the live simulator.

The snapshot carries exactly the fields the simulator's validation rules
read (guide §5.2), so a violation found here is a violation the simulator
would find.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DepotSnapshot:
    """A depot, as the decision engine sees it."""

    id: str
    status: str
    dispatch_capacity_per_tick: float
    inventory: dict[str, float]
    capacity: dict[str, float]


@dataclass(frozen=True, slots=True)
class StationSnapshot:
    """A station, as the decision engine sees it."""

    id: str
    status: str
    inventory: dict[str, float]
    capacity: dict[str, float]


@dataclass(frozen=True, slots=True)
class RouteSnapshot:
    """A route, as the decision engine sees it."""

    id: str
    source_depot_id: str
    destination_station_id: str
    transit_ticks: int
    max_shipment: float
    status: str


@dataclass(frozen=True, slots=True)
class AllocationSnapshot:
    """An allocation already in the simulator's ledger."""

    id: int
    idempotency_key: str
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: str
    quantity: float
    created_tick: int
    expected_arrival_tick: int | None
    status: str


@dataclass(frozen=True, slots=True)
class SupplyArrivalSnapshot:
    """A scheduled delivery of fuel into a depot."""

    id: str
    depot_id: str
    fuel_type: str
    quantity: float
    planned_tick: int
    actual_tick: int | None
    status: str


@dataclass(slots=True)
class WorldSnapshot:
    """A frozen view of the network at one tick."""

    tick: int
    sim_time: datetime | None = None
    tick_minutes: int = 15
    depots: dict[str, DepotSnapshot] = field(default_factory=dict)
    stations: dict[str, StationSnapshot] = field(default_factory=dict)
    routes: tuple[RouteSnapshot, ...] = ()
    allocations: tuple[AllocationSnapshot, ...] = ()
    supply_arrivals: tuple[SupplyArrivalSnapshot, ...] = ()

    def routes_to(self, station_id: str) -> list[RouteSnapshot]:
        """Every route that ends at ``station_id``, fastest first.

        The fastest route is tried first because a station that runs dry in
        two hours does not care that the backup route is quicker to *set
        up* — it cares that the fuel arrives sooner.
        """
        candidates = [route for route in self.routes if route.destination_station_id == station_id]
        return sorted(candidates, key=lambda route: (route.transit_ticks, route.id))

    def in_transit_to(self, station_id: str, fuel_type: str) -> list[AllocationSnapshot]:
        """The fuel already on its way to a station for a fuel."""
        return [
            allocation
            for allocation in self.allocations
            if allocation.destination_station_id == station_id
            and allocation.fuel_type == fuel_type
            and allocation.status in {"PENDING", "IN_TRANSIT"}
        ]

    def committed_dispatch(self, depot_id: str, tick: int) -> float:
        """How much a depot has already committed on this tick.

        The simulator's dispatch allowance is per depot **per tick**: every
        allocation created on the current tick counts against it until the
        tick advances.
        """
        return sum(
            allocation.quantity
            for allocation in self.allocations
            if allocation.source_depot_id == depot_id
            and allocation.created_tick == tick
            and allocation.status in {"PENDING", "IN_TRANSIT"}
        )

    def remaining_dispatch(self, depot_id: str, tick: int) -> float:
        """How much a depot may still dispatch on this tick."""
        depot = self.depots.get(depot_id)
        if depot is None:
            return 0.0
        return max(
            0.0,
            depot.dispatch_capacity_per_tick - self.committed_dispatch(depot_id, tick),
        )

    def incoming_supply(self, depot_id: str, fuel_type: str, until_tick: int) -> float:
        """Fuel scheduled to arrive at a depot within ``until_tick``.

        A shipment that will arrive before the station needs it is not
        something we should count as available *now*, but it is worth
        telling the operator that the fuel is coming.
        """
        return sum(
            arrival.quantity
            for arrival in self.supply_arrivals
            if arrival.depot_id == depot_id
            and arrival.fuel_type == fuel_type
            and arrival.status in {"SCHEDULED", "DELAYED"}
            and arrival.planned_tick <= until_tick
        )
