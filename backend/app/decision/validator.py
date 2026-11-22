"""Local validation of an allocation against the simulator's rules.

This is the pre-flight check: given a proposed (depot, station, route, fuel,
quantity), it decides whether the simulator would accept it, and if not,
which rule it would trip and in what order.

The checks are applied in the guide's documented order (guide §5.2) because
the **first failure wins**. A request that breaks three rules is reported
with the reason the simulator would give, not with the one that happens to
be easiest to detect locally.
"""

from __future__ import annotations

from .constraints import ConstraintCheck, ConstraintCode, ConstraintReport
from .world import WorldSnapshot

#: Depot statuses the simulator accepts allocations from (guide §5.2).
SHIPABLE_DEPOT_STATUSES = frozenset({"OPEN", "CONSTRAINED"})


class AllocationValidator:
    """Checks a proposed allocation against every simulator rule."""

    def validate(
        self,
        *,
        world: WorldSnapshot,
        source_depot_id: str,
        destination_station_id: str,
        route_id: str,
        fuel_type: str,
        quantity: float,
    ) -> ConstraintReport:
        """Validate one allocation, returning every check that was made."""
        checks: list[ConstraintCheck] = []

        route = next((r for r in world.routes if r.id == route_id), None)
        depot = world.depots.get(source_depot_id)
        station = world.stations.get(destination_station_id)

        # 1. ROUTE_MISMATCH — the route must connect the two endpoints.
        if route is None:
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.ROUTE_MISMATCH,
                    satisfied=False,
                    detail=f"route {route_id} does not exist",
                )
            )
        else:
            matches = (
                route.source_depot_id == source_depot_id
                and route.destination_station_id == destination_station_id
            )
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.ROUTE_MISMATCH,
                    satisfied=matches,
                    detail=(
                        f"route {route_id} connects {route.source_depot_id} to "
                        f"{route.destination_station_id}"
                        if matches
                        else (
                            f"route {route_id} connects {route.source_depot_id} to "
                            f"{route.destination_station_id}, not {source_depot_id} "
                            f"to {destination_station_id}"
                        )
                    ),
                )
            )

        # 2. DEPOT_CLOSED — CONSTRAINED depots are still shippable.
        if depot is None:
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.DEPOT_CLOSED,
                    satisfied=False,
                    detail=f"depot {source_depot_id} does not exist",
                )
            )
        else:
            shippable = depot.status in SHIPABLE_DEPOT_STATUSES
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.DEPOT_CLOSED,
                    satisfied=shippable,
                    detail=(
                        f"depot {source_depot_id} is {depot.status}"
                        if not shippable
                        else f"depot {source_depot_id} is {depot.status} and can ship"
                    ),
                )
            )

        # 3. STATION_CLOSED.
        if station is None:
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.STATION_CLOSED,
                    satisfied=False,
                    detail=f"station {destination_station_id} does not exist",
                )
            )
        else:
            open_station = station.status == "OPEN"
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.STATION_CLOSED,
                    satisfied=open_station,
                    detail=(
                        f"station {destination_station_id} is {station.status}"
                        if not open_station
                        else f"station {destination_station_id} is open"
                    ),
                )
            )

        # 4. ROUTE_DISRUPTED.
        if route is None:
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.ROUTE_DISRUPTED,
                    satisfied=False,
                    detail=f"route {route_id} does not exist",
                )
            )
        else:
            available = route.status == "AVAILABLE"
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.ROUTE_DISRUPTED,
                    satisfied=available,
                    detail=(
                        f"route {route_id} is {route.status}"
                        if not available
                        else f"route {route_id} is available"
                    ),
                )
            )

        # 5. ROUTE_CAPACITY_EXCEEDED.
        if route is None:
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.ROUTE_CAPACITY_EXCEEDED,
                    satisfied=False,
                    detail=f"route {route_id} does not exist",
                )
            )
        else:
            within = quantity <= route.max_shipment
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.ROUTE_CAPACITY_EXCEEDED,
                    satisfied=within,
                    detail=(
                        f"{quantity:,.0f} L against a {route.max_shipment:,.0f} L "
                        f"limit on {route_id}"
                    ),
                    observed=quantity,
                    limit=route.max_shipment,
                )
            )

        # 6. INSUFFICIENT_INVENTORY.
        if depot is None:
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.INSUFFICIENT_INVENTORY,
                    satisfied=False,
                    detail=f"depot {source_depot_id} does not exist",
                )
            )
        else:
            stock = depot.inventory.get(fuel_type, 0.0)
            enough = stock >= quantity
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.INSUFFICIENT_INVENTORY,
                    satisfied=enough,
                    detail=(f"depot {source_depot_id} holds {stock:,.0f} L of {fuel_type}"),
                    observed=quantity,
                    limit=stock,
                )
            )

        # 7. DISPATCH_CAPACITY_EXCEEDED — per depot, per tick.
        remaining = world.remaining_dispatch(source_depot_id, world.tick)
        within_dispatch = quantity <= remaining
        dispatch_detail = (
            f"{remaining:,.0f} L of dispatch allowance left at "
            f"{source_depot_id} on tick {world.tick}"
        )
        checks.append(
            ConstraintCheck(
                code=ConstraintCode.DISPATCH_CAPACITY_EXCEEDED,
                satisfied=within_dispatch,
                detail=dispatch_detail,
                observed=quantity,
                limit=remaining,
            )
        )

        # 8. DESTINATION_CAPACITY_EXCEEDED — the tank must not overflow.
        if station is None:
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.DESTINATION_CAPACITY_EXCEEDED,
                    satisfied=False,
                    detail=f"station {destination_station_id} does not exist",
                )
            )
        else:
            on_hand = station.inventory.get(fuel_type, 0.0)
            tank_capacity = station.capacity.get(fuel_type, 0.0)
            room = max(0.0, tank_capacity - on_hand)
            fits = quantity <= room
            checks.append(
                ConstraintCheck(
                    code=ConstraintCode.DESTINATION_CAPACITY_EXCEEDED,
                    satisfied=fits,
                    detail=(
                        f"{room:,.0f} L of room at {destination_station_id} "
                        f"(on hand {on_hand:,.0f} L of {tank_capacity:,.0f} L)"
                    ),
                    observed=quantity,
                    limit=room,
                )
            )

        return ConstraintReport(checks=checks)

    def max_quantity(
        self,
        *,
        world: WorldSnapshot,
        source_depot_id: str,
        destination_station_id: str,
        route_id: str,
        fuel_type: str,
    ) -> float:
        """The largest quantity that would be accepted for this pair.

        This is the quantity cap implied by every capacity constraint at
        once: the route's shipment limit, the depot's stock, the depot's
        remaining dispatch allowance and the station's free tank space.

        The *status* constraints (closed depot, closed station, disrupted
        route) are not caps — they make the allocation impossible outright,
        and are checked separately.
        """
        route = next((r for r in world.routes if r.id == route_id), None)
        depot = world.depots.get(source_depot_id)
        station = world.stations.get(destination_station_id)

        if route is None or depot is None or station is None:
            return 0.0

        return max(
            0.0,
            min(
                route.max_shipment,
                depot.inventory.get(fuel_type, 0.0),
                world.remaining_dispatch(source_depot_id, world.tick),
                max(
                    0.0,
                    station.capacity.get(fuel_type, 0.0) - station.inventory.get(fuel_type, 0.0),
                ),
            ),
        )
