"""Tests for the decision layer: validation and optimization.

The most important tests here are the **409 cases**: each of the
simulator's eight conflict codes is reproduced locally, and the engine has
to predict it *before* the submission rather than after it.
"""

from __future__ import annotations

from decision_fixtures import build_allocation, build_risk, build_world

from app.decision.constraints import ConstraintCode
from app.decision.models import RecommendationStatus
from app.decision.optimizer import AllocationOptimizer
from app.decision.validator import AllocationValidator
from app.decision.world import DepotSnapshot, RouteSnapshot, StationSnapshot
from app.intelligence.risk import RiskLevel

# --- the validator: every 409 the simulator can return --------------------


def test_a_valid_allocation_passes_every_check() -> None:
    validator = AllocationValidator()

    report = validator.validate(
        world=build_world(),
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=3_000.0,
    )

    assert report.is_valid
    assert report.violations == []
    assert len(report.checks) == 8


def test_route_mismatch_is_predicted() -> None:
    """The route must connect the depot and station in the request."""
    validator = AllocationValidator()

    report = validator.validate(
        world=build_world(),
        source_depot_id="depot-patiya",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert not report.is_valid
    assert report.primary_violation.code is ConstraintCode.ROUTE_MISMATCH


def test_depot_closed_is_predicted() -> None:
    """A depot that is neither OPEN nor CONSTRAINED cannot ship."""
    world = build_world()
    depot = world.depots["depot-gazipur"]
    world.depots["depot-gazipur"] = DepotSnapshot(
        id=depot.id,
        status="CLOSED",
        dispatch_capacity_per_tick=depot.dispatch_capacity_per_tick,
        inventory=depot.inventory,
        capacity=depot.capacity,
    )
    validator = AllocationValidator()

    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert report.primary_violation.code is ConstraintCode.DEPOT_CLOSED


def test_a_constrained_depot_can_still_ship() -> None:
    """CONSTRAINED is shippable — refusing it would stop shipping when it matters."""
    world = build_world()
    depot = world.depots["depot-gazipur"]
    world.depots["depot-gazipur"] = DepotSnapshot(
        id=depot.id,
        status="CONSTRAINED",
        dispatch_capacity_per_tick=depot.dispatch_capacity_per_tick,
        inventory=depot.inventory,
        capacity=depot.capacity,
    )
    validator = AllocationValidator()

    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert report.is_valid


def test_station_closed_is_predicted() -> None:
    world = build_world()
    station = world.stations["station-mirpur"]
    world.stations["station-mirpur"] = StationSnapshot(
        id=station.id,
        status="OUTAGE",
        inventory=station.inventory,
        capacity=station.capacity,
    )
    validator = AllocationValidator()

    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert report.primary_violation.code is ConstraintCode.STATION_CLOSED


def test_route_disrupted_is_predicted() -> None:
    world = build_world()
    world.routes = (
        RouteSnapshot(
            id="route-gazipur-mirpur",
            source_depot_id="depot-gazipur",
            destination_station_id="station-mirpur",
            transit_ticks=2,
            max_shipment=7_000.0,
            status="DISRUPTED",
        ),
    )
    validator = AllocationValidator()

    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert report.primary_violation.code is ConstraintCode.ROUTE_DISRUPTED


def test_route_capacity_exceeded_is_predicted() -> None:
    validator = AllocationValidator()

    report = validator.validate(
        world=build_world(),
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=7_000.1,
    )

    assert report.primary_violation.code is ConstraintCode.ROUTE_CAPACITY_EXCEEDED


def test_insufficient_inventory_is_predicted() -> None:
    world = build_world()
    depot = world.depots["depot-gazipur"]
    world.depots["depot-gazipur"] = DepotSnapshot(
        id=depot.id,
        status="OPEN",
        dispatch_capacity_per_tick=depot.dispatch_capacity_per_tick,
        inventory={"DIESEL": 500.0, "PETROL": 45_000.0, "OCTANE": 26_000.0},
        capacity=depot.capacity,
    )
    validator = AllocationValidator()

    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert report.primary_violation.code is ConstraintCode.INSUFFICIENT_INVENTORY


def test_dispatch_capacity_exceeded_is_predicted() -> None:
    """The depot's per-tick allowance counts what it already committed."""
    world = build_world(
        allocations=(build_allocation(quantity=11_000.0, fuel_type="PETROL", created_tick=10),)
    )
    validator = AllocationValidator()

    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=2_000.0,
    )

    assert report.primary_violation.code is ConstraintCode.DISPATCH_CAPACITY_EXCEEDED


def test_dispatch_capacity_from_an_earlier_tick_does_not_count() -> None:
    """The simulator's allowance resets when the tick advances."""
    world = build_world(
        allocations=(build_allocation(quantity=11_000.0, fuel_type="PETROL", created_tick=9),)
    )
    validator = AllocationValidator()

    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=2_000.0,
    )

    assert report.is_valid


def test_destination_capacity_exceeded_is_predicted() -> None:
    """The station's tank must not overflow."""
    world = build_world()
    station = world.stations["station-mirpur"]
    world.stations["station-mirpur"] = StationSnapshot(
        id=station.id,
        status="OPEN",
        inventory={"DIESEL": 14_000.0, "PETROL": 9_000.0, "OCTANE": 5_000.0},
        capacity=station.capacity,
    )
    validator = AllocationValidator()

    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=2_000.0,
    )

    assert report.primary_violation.code is ConstraintCode.DESTINATION_CAPACITY_EXCEEDED


def test_the_first_failure_is_the_one_reported() -> None:
    """The guide says the first failure wins, so that is what we report."""
    world = build_world()
    station = world.stations["station-mirpur"]
    world.stations["station-mirpur"] = StationSnapshot(
        id=station.id,
        status="OUTAGE",
        inventory={"DIESEL": 14_000.0, "PETROL": 9_000.0, "OCTANE": 5_000.0},
        capacity=station.capacity,
    )
    validator = AllocationValidator()

    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=99_000.0,
    )

    # STATION_CLOSED is checked before the capacity rules.
    assert report.primary_violation.code is ConstraintCode.STATION_CLOSED
    assert len(report.violations) > 1


def test_the_max_quantity_is_the_smallest_of_every_cap() -> None:
    validator = AllocationValidator()
    world = build_world()

    cap = validator.max_quantity(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
    )

    station = world.stations["station-mirpur"]
    # The route limit (7,000), the depot stock (60,000), the depot's
    # dispatch allowance (12,000) and the station's room (13,000).
    assert cap == 7_000.0
    assert cap <= station.capacity["DIESEL"] - station.inventory["DIESEL"]


# --- the optimizer ---------------------------------------------------------


def test_the_optimizer_proposes_a_valid_allocation() -> None:
    optimizer = AllocationOptimizer()

    recommendations = optimizer.propose(world=build_world(), risks=[build_risk()])

    assert len(recommendations) == 1
    recommendation = recommendations[0]
    assert recommendation.station_id == "station-mirpur"
    assert recommendation.fuel_type == "DIESEL"
    assert recommendation.source_depot_id == "depot-gazipur"
    assert recommendation.route_id == "route-gazipur-mirpur"
    assert recommendation.quantity > 0
    assert recommendation.status is RecommendationStatus.PROPOSED


def test_the_optimizer_never_proposes_an_invalid_allocation() -> None:
    """Every proposal passes every constraint."""
    optimizer = AllocationOptimizer()

    recommendations = optimizer.propose(world=build_world(), risks=[build_risk()])

    for recommendation in recommendations:
        assert all(check["satisfied"] for check in recommendation.constraints)


def test_the_optimizer_prefers_the_faster_route() -> None:
    """The fast route wins even though the backup is otherwise fine."""
    optimizer = AllocationOptimizer()

    recommendations = optimizer.propose(world=build_world(), risks=[build_risk()])

    assert recommendations[0].route_id == "route-gazipur-mirpur"


def test_the_optimizer_falls_back_to_the_backup_route() -> None:
    """When the fast route is disrupted, the backup is used."""
    world = build_world()
    world.routes = (
        RouteSnapshot(
            id="route-gazipur-mirpur",
            source_depot_id="depot-gazipur",
            destination_station_id="station-mirpur",
            transit_ticks=2,
            max_shipment=7_000.0,
            status="DISRUPTED",
        ),
        RouteSnapshot(
            id="route-patiya-mirpur",
            source_depot_id="depot-patiya",
            destination_station_id="station-mirpur",
            transit_ticks=4,
            max_shipment=5_000.0,
            status="AVAILABLE",
        ),
    )
    optimizer = AllocationOptimizer()

    recommendations = optimizer.propose(world=world, risks=[build_risk()])

    assert len(recommendations) == 1
    assert recommendations[0].route_id == "route-patiya-mirpur"


def test_the_optimizer_records_the_rejected_route_as_an_alternative() -> None:
    """The operator can see the route that was considered and why it lost."""
    world = build_world()
    world.routes = (
        RouteSnapshot(
            id="route-gazipur-mirpur",
            source_depot_id="depot-gazipur",
            destination_station_id="station-mirpur",
            transit_ticks=2,
            max_shipment=7_000.0,
            status="DISRUPTED",
        ),
        RouteSnapshot(
            id="route-patiya-mirpur",
            source_depot_id="depot-patiya",
            destination_station_id="station-mirpur",
            transit_ticks=4,
            max_shipment=5_000.0,
            status="AVAILABLE",
        ),
    )
    optimizer = AllocationOptimizer()

    recommendations = optimizer.propose(world=world, risks=[build_risk()])

    alternatives = recommendations[0].alternatives
    assert len(alternatives) == 1
    assert alternatives[0].route_id == "route-gazipur-mirpur"
    assert "ROUTE_DISRUPTED" in alternatives[0].reason


def test_the_shipment_never_exceeds_the_route_limit() -> None:
    """A shipment over the route's limit would come back as a 409."""
    optimizer = AllocationOptimizer()
    world = build_world()

    recommendations = optimizer.propose(world=world, risks=[build_risk()])

    for recommendation in recommendations:
        route = next(route for route in world.routes if route.id == recommendation.route_id)
        assert recommendation.quantity <= route.max_shipment


def test_the_shipment_never_exceeds_the_depot_stock() -> None:
    optimizer = AllocationOptimizer()
    world = build_world()

    recommendations = optimizer.propose(world=world, risks=[build_risk()])

    for recommendation in recommendations:
        depot = world.depots[recommendation.source_depot_id]
        assert recommendation.quantity <= depot.inventory[recommendation.fuel_type]


def test_the_shipment_never_exceeds_the_station_capacity() -> None:
    optimizer = AllocationOptimizer()
    world = build_world()

    recommendations = optimizer.propose(world=world, risks=[build_risk()])

    for recommendation in recommendations:
        station = world.stations[recommendation.station_id]
        on_hand = station.inventory[recommendation.fuel_type]
        assert on_hand + recommendation.quantity <= station.capacity[recommendation.fuel_type]


def test_the_shipment_never_exceeds_the_depot_dispatch_allowance() -> None:
    optimizer = AllocationOptimizer()
    world = build_world()

    recommendations = optimizer.propose(world=world, risks=[build_risk()])

    for recommendation in recommendations:
        depot = world.depots[recommendation.source_depot_id]
        assert recommendation.quantity <= depot.dispatch_capacity_per_tick


def test_a_station_with_plenty_of_cover_is_not_proposed_for() -> None:
    """An OK station is left alone."""
    optimizer = AllocationOptimizer()
    risk = build_risk(hours_to_stockout=None, inventory=14_000.0, level=RiskLevel.OK)

    recommendations = optimizer.propose(world=build_world(), risks=[risk])

    assert recommendations == []


def test_the_idempotency_key_is_deterministic() -> None:
    """The same proposal must produce the same key, so a retry is a replay."""
    optimizer = AllocationOptimizer()

    first = optimizer.propose(world=build_world(), risks=[build_risk()])
    second = optimizer.propose(world=build_world(), risks=[build_risk()])

    assert first[0].idempotency_key == second[0].idempotency_key


def test_the_projection_shows_the_risk_improving() -> None:
    """The recommendation says what it would achieve."""
    optimizer = AllocationOptimizer()

    recommendation = optimizer.propose(world=build_world(), risks=[build_risk()])[0]

    projection = recommendation.projection
    assert projection is not None
    assert projection.hours_to_stockout_after is None or (
        projection.hours_to_stockout_after > (projection.hours_to_stockout_before or 0.0)
    )
    assert projection.stockout_probability_after <= projection.stockout_probability_before


def test_a_critical_recommendation_always_needs_a_human() -> None:
    """CRITICAL is exactly the case where a human should look."""
    optimizer = AllocationOptimizer()

    recommendation = optimizer.propose(world=build_world(), risks=[build_risk()])[0]

    assert recommendation.risk_level == "CRITICAL"
    assert recommendation.needs_human_review is True


def test_the_recommendation_carries_its_signals_and_constraints() -> None:
    """The operator sees why the engine decided this."""
    optimizer = AllocationOptimizer()

    recommendation = optimizer.propose(world=build_world(), risks=[build_risk()])[0]

    assert recommendation.signals
    assert len(recommendation.constraints) == 8
    assert recommendation.rationale
    assert 0.0 <= recommendation.confidence <= 1.0


def test_the_slow_route_is_scored_honestly() -> None:
    """A slow route cannot claim cover the station cannot use yet."""
    optimizer = AllocationOptimizer()
    world = build_world()
    world.routes = (
        RouteSnapshot(
            id="route-patiya-mirpur",
            source_depot_id="depot-patiya",
            destination_station_id="station-mirpur",
            transit_ticks=4,
            max_shipment=5_000.0,
            status="AVAILABLE",
        ),
    )

    recommendation = optimizer.propose(world=world, risks=[build_risk()])[0]

    projection = recommendation.projection
    assert projection is not None
    assert projection.arrival_tick == world.tick + 4
    assert projection.hours_until_arrival == 1.0


def test_the_fallback_policy_uses_a_fixed_confidence() -> None:
    """The fallback has no model behind it, so it does not claim confidence."""
    optimizer = AllocationOptimizer()

    recommendation = optimizer.propose(world=build_world(), risks=[build_risk()], fallback=True)[0]

    assert recommendation.used_fallback_policy is True
    assert recommendation.confidence <= 0.4
    assert recommendation.needs_human_review is True
    assert "lowest days of cover" in recommendation.rationale
